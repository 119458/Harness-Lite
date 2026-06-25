"""五层上下文管理系统完整单元测试。

覆盖范围：
- L1 DiskOffloadLayer / LargeResultStore
- L2 SnipLayer
- L3 TimeDecayLayer
- L4 ContextCollapse
- L5 AutoCompactLayer
- parse_summary_block
- anchors (find_safe_cut_points / _validate_pairs)
- CompactPipeline 级联
- DynamicContextManager 门面
- Engine 集成
"""
from __future__ import annotations

import asyncio
import copy
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 被测模块 — 从包级 __init__.py 导出
# ---------------------------------------------------------------------------
from harness_lite.context.compact import (
    AUTOCOMPACT_BUFFER_TOKENS,
    COMPACTABLE_TOOLS,
    KEEP_RECENT_TOOL_RESULTS,
    LARGE_RESULT_PREVIEW_CHARS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    META_ID_KEY,
    TIME_DECAY_PROACTIVE_RATIO,
    AutoCompactLayer,
    CompactionResult,
    CompactPipeline,
    ContextCollapse,
    DiskOffloadLayer,
    LargeResultStore,
    MessageMeta,
    SnipLayer,
    TimeDecayLayer,
    TokenCounter,
    find_safe_cut_points,
    parse_summary_block,
)
# ---------------------------------------------------------------------------
# 从子模块导入 __init__.py 未导出的内部常量/函数
# ---------------------------------------------------------------------------
from harness_lite.context.compact.auto_compact import (
    ARCHIVE_PREFIX,
    DEGRADED_PREFIX,
)
from harness_lite.context.compact.local_layers import (
    L3_PLACEHOLDER_PREFIX,
    _collect_compactable_tool_call_ids,
    _validate_pairs,
)
from harness_lite.context.compact.storage import (
    LARGE_RESULT_STUB_PREFIX,
    LARGE_RESULT_THRESHOLD_BYTES,
    _sanitize_session_id,
    _validate_ref_id,
)
from harness_lite.context.manager import DynamicContextManager


# ============================================================================
# 构造辅助
# ============================================================================

def _tc(tool_call_id: str, name: str = "read_file") -> Dict[str, Any]:
    """构造单条 tool_calls 元素。"""
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _tool_msg(tool_call_id: str, content: str = "ok", name: str = "read_file") -> Dict[str, Any]:
    """构造 tool 角色消息。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content, "name": name}


def _assistant_msg(tool_calls: List[Dict[str, Any]] | None = None,
                   content: str = "",
                   reasoning: str | None = None) -> Dict[str, Any]:
    """构造 assistant 角色消息。"""
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return msg


def _user_msg(content: str = "hello") -> Dict[str, Any]:
    """构造 user 角色消息。"""
    return {"role": "user", "content": content}


def _system_msg(content: str = "system prompt") -> Dict[str, Any]:
    """构造 system 角色消息。"""
    return {"role": "system", "content": content}


def _make_messages_with_tools(
    n_tools: int = 8,
    n_keep: int = 5,
) -> tuple:
    """构造含 n_tools 条 tool 消息的对话历史。

    返回 (messages, sidecar, token_counter)。
    """
    tc = TokenCounter()
    messages: List[Dict[str, Any]] = [_system_msg()]
    sidecar: Dict[str, MessageMeta] = {}
    now = datetime.now()

    for i in range(n_tools):
        tcid = f"tc_{i:03d}"
        messages.append(_user_msg(f"request {i}"))
        messages.append(_assistant_msg(tool_calls=[_tc(tcid)]))
        tool = _tool_msg(tcid, content=f"result {i} " * 50)
        mid = f"meta_{i:03d}"
        tool["_meta_id"] = mid
        messages.append(tool)
        sidecar[mid] = MessageMeta(
            created_at=now - timedelta(minutes=n_tools - i),
            last_seen_at=now - timedelta(minutes=n_tools - i),
        )

    return messages, sidecar, tc


def _make_paired_messages() -> List[Dict[str, Any]]:
    """构造完整的 assistant.tool_calls + tool 配对消息序列。"""
    return [
        _system_msg(),
        _user_msg("do stuff"),
        _assistant_msg(tool_calls=[_tc("call_001"), _tc("call_002")]),
        _tool_msg("call_001", "result 1"),
        _tool_msg("call_002", "result 2"),
        _assistant_msg(content="done"),
        _user_msg("next"),
    ]


# ============================================================================
# TestL1DiskOffload
# ============================================================================

class TestL1DiskOffload:
    """L1 大结果落盘层测试。"""

    def test_small_result_passes_through(self):
        """<50KB tool 消息不落盘，ref=None。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LargeResultStore(base_dir=Path(tmpdir))
            l1 = DiskOffloadLayer(store)
            msg = _tool_msg("tc_001", "small content")
            new_msg, ref = l1.maybe_offload(msg, "sess1")
            assert ref is None
            assert new_msg is msg  # 同一对象，无改动

    def test_large_result_offloaded(self):
        """60KB 触发落盘，ref 非空，磁盘文件存在，content 以 STUB_PREFIX 开头。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LargeResultStore(base_dir=Path(tmpdir))
            l1 = DiskOffloadLayer(store)
            big_content = "x" * 60_000
            msg = _tool_msg("tc_big", big_content)
            new_msg, ref = l1.maybe_offload(msg, "sess1")
            assert ref is not None
            assert ref.byte_size >= 60_000
            assert new_msg["content"].startswith(LARGE_RESULT_STUB_PREFIX)
            # 磁盘文件存在
            disk_path = Path(ref.disk_path)
            assert disk_path.exists()
            # 磁盘内容与原文一致
            assert disk_path.read_text(encoding="utf-8") == big_content

    def test_idempotent(self):
        """对存根再次 maybe_offload -> 不再落盘。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LargeResultStore(base_dir=Path(tmpdir))
            l1 = DiskOffloadLayer(store)
            big_content = "y" * 60_000
            msg = _tool_msg("tc_idem", big_content)
            new_msg, ref1 = l1.maybe_offload(msg, "sess1")
            assert ref1 is not None
            # 第二次：内容已是存根
            new_msg2, ref2 = l1.maybe_offload(new_msg, "sess1")
            assert ref2 is None

    def test_path_traversal_session_id_rejected(self):
        """session_id='..' -> ValueError。"""
        with pytest.raises(ValueError, match="session_id"):
            _sanitize_session_id("..")

    def test_invalid_ref_id_rejected(self):
        """ref_id 含换行/空 -> ValueError。"""
        with pytest.raises(ValueError):
            _validate_ref_id("abc\n123")
        with pytest.raises(ValueError):
            _validate_ref_id("")
        with pytest.raises(ValueError):
            _validate_ref_id("  ")

    def test_ref_id_collision_resistant(self):
        """同 content 不同 tool_call_id -> 不同 ref_id。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LargeResultStore(base_dir=Path(tmpdir))
            l1 = DiskOffloadLayer(store)
            big_content = "z" * 60_000
            msg1 = _tool_msg("tc_A", big_content)
            msg2 = _tool_msg("tc_B", big_content)
            _, ref1 = l1.maybe_offload(msg1, "sess1")
            _, ref2 = l1.maybe_offload(msg2, "sess1")
            assert ref1 is not None
            assert ref2 is not None
            assert ref1.ref_id != ref2.ref_id

    def test_cleanup_session(self):
        """write 后 cleanup_session -> 目录消失。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LargeResultStore(base_dir=Path(tmpdir))
            l1 = DiskOffloadLayer(store)
            big_content = "w" * 60_000
            msg = _tool_msg("tc_clean", big_content)
            l1.maybe_offload(msg, "sess_clean")
            # session 目录存在
            session_dir = store._session_dir("sess_clean")
            assert session_dir.exists()
            # 清理
            store.cleanup_session("sess_clean")
            assert not session_dir.exists()


# ============================================================================
# TestL2Snip
# ============================================================================

class TestL2Snip:
    """L2 手动截断层测试。"""

    def test_safe_indices_pass(self):
        """snip user 消息 -> success, saved_tokens>0, sidecar 清理。"""
        messages, sidecar, tc = _make_messages_with_tools(n_tools=2)
        # 找到一条 user 消息的索引并 snip
        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        assert len(user_indices) >= 1
        idx = user_indices[0]
        # 检查 snip 该 user 不会破坏对完整性（需要同时删对应 tool 对）
        # 这里只 snip 一条 user 消息，若不破坏对则成功
        l2 = SnipLayer()
        r = l2.apply(messages, [idx], sidecar, tc)
        # user 消息不在任何 tool pair 中，删除不应破坏对完整性
        assert r.success is True
        assert r.saved_tokens > 0
        # sidecar 中被删消息的 meta 应已清理
        snipped_mid = messages[idx].get("_meta_id")
        if snipped_mid:
            assert snipped_mid not in sidecar

    def test_break_pair_rejected(self):
        """snip tool 消息（破坏对）-> success=False。"""
        messages = _make_paired_messages()
        sidecar: Dict[str, MessageMeta] = {}
        tc = TokenCounter()
        l2 = SnipLayer()
        # tool 消息索引 3（call_001 的响应）
        r = l2.apply(messages, [3], sidecar, tc)
        assert r.success is False
        assert "工具对" in r.reason

    def test_out_of_range_indices(self):
        """越界 -> success=False。"""
        messages = _make_paired_messages()
        sidecar: Dict[str, MessageMeta] = {}
        tc = TokenCounter()
        l2 = SnipLayer()
        r = l2.apply(messages, [999], sidecar, tc)
        assert r.success is False
        assert "越界" in r.reason

    def test_empty_indices(self):
        """空 -> skipped=True。"""
        messages = _make_paired_messages()
        sidecar: Dict[str, MessageMeta] = {}
        tc = TokenCounter()
        l2 = SnipLayer()
        r = l2.apply(messages, [], sidecar, tc)
        assert r.skipped is True


# ============================================================================
# TestL3TimeDecay
# ============================================================================

class TestL3TimeDecay:
    """L3 时间衰减层测试。"""

    def test_keep_recent_n_tools(self):
        """8 条 tool -> 保留最近 5，前 3 变占位符。"""
        messages, sidecar, tc = _make_messages_with_tools(n_tools=8)
        l3 = TimeDecayLayer()
        r = l3.apply(messages, sidecar, tc)
        assert r.success is True
        assert r.details["cleared_tools"] == 3  # 8 - 5 = 3
        # 验证前 3 条 tool 消息变成占位符
        after = r.messages_after
        tool_msgs = [m for m in after if m.get("role") == "tool"]
        placeholder_count = sum(
            1 for m in tool_msgs
            if (m.get("content") or "").startswith(L3_PLACEHOLDER_PREFIX)
        )
        assert placeholder_count == 3

    def test_strip_reasoning_on_old_assistant(self):
        """早期 assistant 的 reasoning_content 被剥离。"""
        tc = TokenCounter()
        messages: List[Dict[str, Any]] = [_system_msg()]
        sidecar: Dict[str, MessageMeta] = {}
        now = datetime.now()

        # 6 条 tool 对（超过 KEEP_RECENT_TOOL_RESULTS=5）
        for i in range(6):
            tcid = f"rc_{i:03d}"
            messages.append(_user_msg(f"ask {i}"))
            messages.append(_assistant_msg(
                tool_calls=[_tc(tcid)],
                reasoning=f"thinking {i}",
            ))
            tool = _tool_msg(tcid, f"result {i}")
            mid = f"meta_rc_{i:03d}"
            tool["_meta_id"] = mid
            messages.append(tool)
            sidecar[mid] = MessageMeta(
                created_at=now - timedelta(minutes=6 - i),
                last_seen_at=now - timedelta(minutes=6 - i),
            )

        l3 = TimeDecayLayer()
        r = l3.apply(messages, sidecar, tc)
        assert r.success is True
        assert r.details["stripped_reasonings"] >= 1
        # 早期 assistant 不再含 reasoning_content
        early_assts = [
            m for m in r.messages_after
            if m.get("role") == "assistant"
            and m.get("tool_calls")
        ]
        # 最早的 assistant 应已剥离 reasoning
        has_early_stripped = any(
            "reasoning_content" not in m for m in early_assts
        )
        assert has_early_stripped

    def test_idempotent(self):
        """连续两次 apply -> 第二次 saved_tokens=0。"""
        messages, sidecar, tc = _make_messages_with_tools(n_tools=8)
        l3 = TimeDecayLayer()
        r1 = l3.apply(messages, sidecar, tc)
        assert r1.success is True
        assert r1.saved_tokens > 0
        # 用第一次的结果再做一次
        r2 = l3.apply(r1.messages_after, sidecar, tc)
        assert r2.saved_tokens == 0

    def test_should_trigger_proactive(self):
        """token 超阈值 -> True。"""
        messages, sidecar, tc = _make_messages_with_tools(n_tools=8)
        current_tokens = tc.count_messages(messages)
        l3 = TimeDecayLayer()
        # 设置 max_allowed 足够低让 current_tokens 超过 60%
        max_allowed = int(current_tokens * 0.5)
        assert l3.should_trigger(messages, sidecar, current_tokens, max_allowed) is True

    def test_should_trigger_below_thresholds(self):
        """都不超 -> False。"""
        messages = _make_paired_messages()
        sidecar: Dict[str, MessageMeta] = {}
        tc = TokenCounter()
        current_tokens = tc.count_messages(messages)
        l3 = TimeDecayLayer()
        # 设置 max_allowed 极大，token 远低于阈值
        # 且 sidecar 为空（无 last_seen_at），时间条件也不触发
        max_allowed = current_tokens * 100
        assert l3.should_trigger(messages, sidecar, current_tokens, max_allowed) is False

    def test_no_compactable_tools(self):
        """无可压缩工具 -> skipped。"""
        tc = TokenCounter()
        messages = [
            _system_msg(),
            _user_msg("hi"),
            _assistant_msg(content="hello"),
        ]
        sidecar: Dict[str, MessageMeta] = {}
        l3 = TimeDecayLayer()
        r = l3.apply(messages, sidecar, tc)
        assert r.skipped is True


# ============================================================================
# TestL4ContextCollapse
# ============================================================================

class TestL4ContextCollapse:
    """L4 读时投影层测试。"""

    def test_strip_meta_id(self):
        """投影后 _meta_id 消失。"""
        l4 = ContextCollapse()
        messages = [
            {**_system_msg(), "_meta_id": "abc123"},
            _user_msg("hi"),
        ]
        projected = l4.project(messages, thinking_mode=False)
        for m in projected:
            assert "_meta_id" not in m

    def test_strip_reasoning_when_disabled(self):
        """thinking_mode=False 时 reasoning_content 被剥。"""
        l4 = ContextCollapse()
        messages = [
            _system_msg(),
            _assistant_msg(content="hi", reasoning="deep thought"),
        ]
        projected = l4.project(messages, thinking_mode=False)
        for m in projected:
            assert "reasoning_content" not in m

    def test_keep_reasoning_when_enabled(self):
        """thinking_mode=True 时保留。"""
        l4 = ContextCollapse()
        messages = [
            _system_msg(),
            _assistant_msg(content="hi", reasoning="deep thought"),
        ]
        projected = l4.project(messages, thinking_mode=True)
        asst = [m for m in projected if m.get("role") == "assistant"]
        assert len(asst) == 1
        assert asst[0].get("reasoning_content") == "deep thought"

    def test_merge_consecutive_system(self):
        """连续 system 合并。"""
        l4 = ContextCollapse()
        messages = [
            _system_msg("part1"),
            _system_msg("part2"),
            _user_msg("hi"),
        ]
        projected = l4.project(messages, thinking_mode=False)
        assert len(projected) == 2  # 1 merged system + 1 user
        assert "part1" in projected[0]["content"]
        assert "part2" in projected[0]["content"]

    def test_strip_empty_tool_calls(self):
        """assistant.tool_calls=[] 或 None 被 pop。"""
        l4 = ContextCollapse()
        messages = [
            _system_msg(),
            _assistant_msg(content="no tools", reasoning=None),
        ]
        # 手动设空 tool_calls
        messages[1]["tool_calls"] = []
        projected = l4.project(messages, thinking_mode=False)
        assert "tool_calls" not in projected[1]

        # None 的情况
        messages[1]["tool_calls"] = None
        projected = l4.project(messages, thinking_mode=False)
        assert "tool_calls" not in projected[1]

    def test_no_writeback_tool_calls(self):
        """修改投影 tool_calls 不污染原消息（deepcopy 测试）。"""
        l4 = ContextCollapse()
        original_calls = [_tc("orig_001")]
        messages = [
            _system_msg(),
            _assistant_msg(tool_calls=original_calls, content="hi"),
            _tool_msg("orig_001", "result"),
        ]
        projected = l4.project(messages, thinking_mode=False)
        # 修改投影中的 tool_calls
        projected[1]["tool_calls"][0]["id"] = "MUTATED"
        # 原消息不受影响
        assert messages[1]["tool_calls"][0]["id"] == "orig_001"

    def test_strip_is_meta_field(self):
        """投影后 is_meta 内部字段消失，确保发往 SDK 的消息合法。"""
        l4 = ContextCollapse()
        messages = [
            _system_msg(),
            {
                "role": "system",
                "content": "## 本轮可能相关的长期记忆 ...",
                "is_meta": True,
            },
            _user_msg("hi"),
        ]
        projected = l4.project(messages, thinking_mode=False)
        for m in projected:
            assert "is_meta" not in m
        # 同时验证：content 仍保留（is_meta 是字段过滤，不是整条丢弃）
        joined = "".join((m.get("content") or "") for m in projected)
        assert "本轮可能相关" in joined

    def test_empty_messages(self):
        """空列表 -> 空列表。"""
        l4 = ContextCollapse()
        projected = l4.project([], thinking_mode=False)
        assert projected == []


# ============================================================================
# TestL5AutoCompact
# ============================================================================

class TestL5AutoCompact:
    """L5 全量结构化摘要层测试。"""

    def test_should_apply_threshold(self):
        """token 超阈值 -> True。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        current_tokens = 100_000
        max_allowed = 50_000
        assert l5.should_apply(current_tokens, max_allowed, snip_freed_tokens=0) is True

    def test_should_apply_circuit_broken(self):
        """_consecutive_failures=3 -> False。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        l5._consecutive_failures = 3
        assert l5.should_apply(999_999, 1, snip_freed_tokens=0) is False

    def test_find_last_user_index(self):
        """末尾有/无 user。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        # 有 user
        msgs = [_system_msg(), _user_msg("q1"), _assistant_msg(content="a1"), _user_msg("q2")]
        assert l5.find_last_user_index(msgs) == 3
        # 无 user
        msgs2 = [_system_msg(), _assistant_msg(content="a")]
        assert l5.find_last_user_index(msgs2) == 1

    def test_enforce_pair_integrity_orphan(self):
        """孤悬 assistant 推回 active_turn。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        # compressible 末尾是 assistant.tool_calls 但缺少 tool 响应
        compressible = [
            _user_msg("q1"),
            _assistant_msg(content="a1"),
            _assistant_msg(tool_calls=[_tc("orphan_001")]),
        ]
        active = [_user_msg("q2")]
        comp, act = l5._enforce_pair_integrity(compressible, active)
        # 孤立 assistant 被推到 active_turn
        assert len(comp) < len(compressible)
        assert act[0].get("role") == "assistant"

    def test_apply_too_short(self):
        """messages<4 -> skipped。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        msgs = [_system_msg(), _user_msg("hi")]
        r = asyncio.run(l5.apply(msgs, engine=None, current_cwd="/tmp"))
        assert r.skipped is True

    def test_apply_success_mock_llm(self):
        """mock _call_llm_summarize 返回标准格式 -> 成功。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        # 构造足够长的消息（>=4 条）
        msgs = [
            _system_msg(),
            _user_msg("q1"),
            _assistant_msg(content="a1"),
            _user_msg("q2"),
            _assistant_msg(content="a2"),
            _user_msg("q3"),
        ]
        summary_text = (
            "<analysis>分析过程</analysis>"
            "<summary>"
            "1.主要请求\n2.技术概念\n3.文件\n4.错误\n5.解决"
            "\n6.用户消息\n7.待办\n8.当前\n9.后续"
            "</summary>"
        )
        with patch.object(l5, "_call_llm_summarize", new_callable=AsyncMock, return_value=summary_text):
            r = asyncio.run(
                l5.apply(msgs, engine=None, current_cwd="/tmp", force=True)
            )
        assert r.success is True
        assert r.messages_after is not None
        # 应含归档 system 消息
        archive_msgs = [m for m in r.messages_after if ARCHIVE_PREFIX in (m.get("content") or "")]
        assert len(archive_msgs) == 1
        assert l5._consecutive_failures == 0

    def test_apply_llm_failure_increments(self):
        """mock 抛错 -> _consecutive_failures+1。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        msgs = [
            _system_msg(),
            _user_msg("q1"),
            _assistant_msg(content="a1"),
            _user_msg("q2"),
        ]
        with patch.object(l5, "_call_llm_summarize", new_callable=AsyncMock, side_effect=RuntimeError("LLM down")):
            asyncio.run(l5.apply(msgs, engine=None, current_cwd="/tmp", force=True))
        assert l5._consecutive_failures == 1

    def test_circuit_breaks_after_3(self):
        """3 次失败 -> 熔断。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        l5._consecutive_failures = 3
        # should_apply 在熔断后永远返回 False
        assert l5.should_apply(999_999, 1, snip_freed_tokens=0) is False

    def test_force_degraded_after_break(self):
        """熔断 + force -> layer="L5-degraded"。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        l5._consecutive_failures = 2  # 再一次失败达到 3
        msgs = [
            _system_msg(),
            _user_msg("q1"),
            _assistant_msg(content="a1"),
            _user_msg("q2"),
        ]
        with patch.object(l5, "_call_llm_summarize", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            r = asyncio.run(
                l5.apply(msgs, engine=None, current_cwd="/tmp", force=True)
            )
        assert r.layer == "L5-degraded"
        assert DEGRADED_PREFIX in (r.messages_after[1].get("content") or "")

    def test_force_skips_threshold(self):
        """force=True 不看阈值。"""
        tc = TokenCounter()
        l5 = AutoCompactLayer(tc)
        msgs = [
            _system_msg(),
            _user_msg("q1"),
            _assistant_msg(content="a1"),
            _user_msg("q2"),
            _assistant_msg(content="a2"),
            _user_msg("q3"),
        ]
        summary_text = "<summary>1.a 2.b 3.c 4.d 5.e 6.f 7.g 8.h 9.i</summary>"
        with patch.object(l5, "_call_llm_summarize", new_callable=AsyncMock, return_value=summary_text):
            # 即使 token 很少，force=True 仍执行
            r = asyncio.run(
                l5.apply(msgs, engine=None, current_cwd="/tmp", force=True)
            )
        assert r.success is True


# ============================================================================
# TestParseSummaryBlock
# ============================================================================

class TestParseSummaryBlock:
    """摘要块解析器测试。"""

    def test_strip_analysis_extract_summary(self):
        """标准格式。"""
        raw = "<analysis>思考过程</analysis><summary>最终内容</summary>"
        result = parse_summary_block(raw)
        assert result == "最终内容"

    def test_no_summary_tag(self):
        """缺 summary -> 返回剩余文本。"""
        raw = "<analysis>分析</analysis>剩余文本"
        result = parse_summary_block(raw)
        assert "剩余文本" in result
        assert "分析" not in result

    def test_empty_input(self):
        """空字符串 -> ''。"""
        assert parse_summary_block("") == ""

    def test_nested_summary_in_analysis(self):
        """analysis 内含 summary 字面量 -> 不影响外部 summary 提取。"""
        raw = (
            "<analysis>这里有个 <summary>假内容</summary> 字面量</analysis>"
            "<summary>真正内容</summary>"
        )
        result = parse_summary_block(raw)
        assert "真正内容" in result
        assert "假内容" not in result


# ============================================================================
# TestAnchors
# ============================================================================

class TestAnchors:
    """锚点与对完整性校验测试。"""

    def test_find_safe_cut_points_clean(self):
        """无 tool_calls -> [0..len]。"""
        msgs = [_system_msg(), _user_msg("hi"), _assistant_msg(content="a")]
        points = find_safe_cut_points(msgs)
        assert points == [0, 1, 2, 3]

    def test_find_safe_cut_points_with_pairs(self):
        """含工具对 -> 跳过对中间。"""
        msgs = _make_paired_messages()
        points = find_safe_cut_points(msgs)
        # 在 assistant(2) + tool(3) + tool(4) 对中间不应有切割点
        # 但 0, 1, 2(assistant前) 之后跳到 5(tool对之后)
        assert 3 not in points
        assert 4 not in points
        assert 5 in points

    def test_validate_pairs_clean(self):
        """合法 -> True。"""
        msgs = _make_paired_messages()
        assert _validate_pairs(msgs) is True

    def test_validate_pairs_break(self):
        """assistant 后跟 user -> False。"""
        msgs = [
            _system_msg(),
            _assistant_msg(tool_calls=[_tc("brk_001")]),
            _user_msg("wrong"),  # 应该是 tool 响应
        ]
        assert _validate_pairs(msgs) is False


# ============================================================================
# TestCompactPipelineCascade
# ============================================================================

class TestCompactPipelineCascade:
    """CompactPipeline 级联测试。"""

    def test_no_overflow_no_compression(self):
        """低 token -> 原样返回。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=999_999,
                memory_store_dir=tmpdir,
            )
            msgs = _make_paired_messages()
            result = asyncio.run(
                pipeline.compress_if_overflow(msgs, engine=None, current_cwd="/tmp")
            )
            assert result is msgs  # 原样返回

    def test_l3_fires_when_proactive(self):
        """token > 60% -> L3 触发。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=500,  # 极低阈值
                memory_store_dir=tmpdir,
            )
            msgs, sidecar, _ = _make_messages_with_tools(n_tools=8)
            # 把 sidecar 注入 pipeline
            pipeline._sidecar = sidecar
            result = asyncio.run(
                pipeline.compress_if_overflow(msgs, engine=None, current_cwd="/tmp")
            )
            # L3 应该被触发
            assert pipeline.stats.l3_apply_count >= 1

    def test_l3_then_l5_cascade(self):
        """L3 释放后仍超 -> L5 也触发（验证 mock 被调 1 次）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=100,  # 极低
                memory_store_dir=tmpdir,
            )
            msgs, sidecar, _ = _make_messages_with_tools(n_tools=8)
            pipeline._sidecar = sidecar
            summary_text = "<summary>1.a 2.b 3.c 4.d 5.e 6.f 7.g 8.h 9.i</summary>"
            with patch.object(
                pipeline.l5, "_call_llm_summarize",
                new_callable=AsyncMock, return_value=summary_text,
            ) as mock_llm:
                asyncio.run(
                    pipeline.compress_if_overflow(msgs, engine=None, current_cwd="/tmp")
                )
                mock_llm.assert_called_once()

    def test_snip_freed_affects_l5(self):
        """snip 释放 tokens -> L5 阈值正确折算。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=5000,
                memory_store_dir=tmpdir,
            )
            # 构造消息
            msgs, sidecar, _ = _make_messages_with_tools(n_tools=3)
            pipeline._sidecar = sidecar
            # snip 一条 user 消息
            user_indices = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
            if user_indices:
                pipeline.snip(msgs, [user_indices[0]])
            # snip_freed_tokens > 0
            assert pipeline._snip_freed_tokens > 0

    def test_record_tool_result_writes_sidecar(self):
        """调用后 sidecar 有条目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=999_999,
                memory_store_dir=tmpdir,
            )
            tool_msg = _tool_msg("rec_001", "small result")
            result_msg = pipeline.record_tool_result(tool_msg, "sess_rec")
            # sidecar 应有条目
            meta_id = result_msg.get(META_ID_KEY)
            assert meta_id is not None
            assert meta_id in pipeline._sidecar

    def test_reset_session_clears_state(self):
        """reset 后全清。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = CompactPipeline(
                max_allowed_tokens=999_999,
                memory_store_dir=tmpdir,
            )
            tool_msg = _tool_msg("rst_001", "result")
            pipeline.record_tool_result(tool_msg, "sess_rst")
            assert len(pipeline._sidecar) > 0
            pipeline.reset_session("test")
            assert len(pipeline._sidecar) == 0
            assert pipeline._snip_freed_tokens == 0


# ============================================================================
# TestDynamicContextManagerFacade
# ============================================================================

class TestDynamicContextManagerFacade:
    """DynamicContextManager 兼容层测试。"""

    def test_legacy_methods(self):
        """calculate_messages_tokens / compress_if_overflow 可调。"""
        dcm = DynamicContextManager(max_allowed_tokens=999_999)
        msgs = _make_paired_messages()
        tokens = dcm.calculate_messages_tokens(msgs)
        assert tokens > 0
        # compress_if_overflow 是 async，需要在事件循环中调用
        result = asyncio.run(
            dcm.compress_if_overflow(msgs, engine=None, current_cwd="/tmp")
        )
        assert isinstance(result, list)

    def test_pipeline_property(self):
        """dcm.pipeline 是 CompactPipeline。"""
        dcm = DynamicContextManager()
        assert isinstance(dcm.pipeline, CompactPipeline)


# ============================================================================
# TestEngineIntegration
# ============================================================================

class TestEngineIntegration:
    """Engine/Strategy 集成测试。"""

    def test_strategy_has_pipeline(self):
        """ReActStrategy().context_manager.pipeline 可访问。"""
        from harness_lite.loop.strategy import ReActStrategy
        strategy = ReActStrategy()
        assert hasattr(strategy, "context_manager")
        assert hasattr(strategy.context_manager, "pipeline")
        assert isinstance(strategy.context_manager.pipeline, CompactPipeline)
