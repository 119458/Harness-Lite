"""
L2 SnipLayer 与 L3 TimeDecayLayer：纯本地的两层减负实现。

L2：根据外部传入的索引列表物理删除消息，删前用工具对完整性校验拦截破坏性删除。
L3：时间衰减 / token 比例触发，把早期 tool 消息内容替换为占位符、剥离早期
    assistant 的 reasoning_content，从而在不调 LLM 的前提下释放可观 token。

两层均保证幂等：重复 apply 不会破坏已经压缩过的消息。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from harness_lite.context.compact.types import (
    CompactionResult,
    MessageMeta,
    TokenCounter,
)

logger = logging.getLogger("harness_lite.compact")


COMPACTABLE_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "grep_search",
        "create_file",
        "edit_file",
        "bash_terminal",
        "python_interpreter",
        "intelligence_search",
        "web_scraper",
        "read_skill",
    }
)
KEEP_RECENT_TOOL_RESULTS = 5
GAP_THRESHOLD_MINUTES = 60
TIME_DECAY_PROACTIVE_RATIO = 0.6
L3_PLACEHOLDER_PREFIX = "[⏳ 早期工具输出已自动清理"


# ---------------------------------------------------------------------------
# 共享辅助函数
# ---------------------------------------------------------------------------

def _collect_compactable_tool_call_ids(messages: List[Dict[str, Any]]) -> List[str]:
    """按出现顺序收集所有 COMPACTABLE_TOOLS 集合内工具调用的 tool_call_id。"""
    ids: List[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls") or []
        for tc in tool_calls:
            func = tc.get("function", {}) or {}
            if func.get("name") in COMPACTABLE_TOOLS:
                tcid = tc.get("id")
                if tcid:
                    ids.append(tcid)
    return ids


def _lookup_tool_name_by_call_id(messages: List[Dict[str, Any]], tool_call_id: str) -> str:
    """反查 tool_call_id 对应的工具名（在 assistant.tool_calls 里找）。"""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls") or []
        for tc in tool_calls:
            if tc.get("id") == tool_call_id:
                return (tc.get("function") or {}).get("name", "unknown")
    return "unknown"


def _validate_pairs(messages: List[Dict[str, Any]]) -> bool:
    """OpenAI 工具对完整性校验。

    每个 assistant.tool_calls[i].id 必须有紧跟的 role=tool tool_call_id 配对，
    中间不能插入其他 role 消息（否则 OpenAI SDK 会 400）。
    """
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            expected_ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            j = i + 1
            for eid in expected_ids:
                if j >= n:
                    return False
                next_m = messages[j]
                if next_m.get("role") != "tool" or next_m.get("tool_call_id") != eid:
                    return False
                j += 1
            i = j
        else:
            i += 1
    return True


# ---------------------------------------------------------------------------
# L2 SnipLayer
# ---------------------------------------------------------------------------

class SnipLayer:
    """L2：手动截断。物理删除指定索引的消息，删除前校验工具对完整性。"""

    def apply(
        self,
        messages: List[Dict[str, Any]],
        indices: List[int],
        sidecar: Dict[str, MessageMeta],
        token_counter: TokenCounter,
    ) -> CompactionResult:
        if not indices:
            return CompactionResult(skipped=True, layer="L2", reason="indices 为空")

        target = sorted(set(indices), reverse=True)
        if any(i < 0 or i >= len(messages) for i in target):
            return CompactionResult(success=False, layer="L2", reason="索引越界")

        sim = [m for i, m in enumerate(messages) if i not in target]
        if not _validate_pairs(sim):
            return CompactionResult(
                success=False, layer="L2", reason="会破坏工具对完整性"
            )

        freed = sum(token_counter.count_message(messages[i]) for i in target)
        for i in target:
            mid = messages[i].get("_meta_id")
            if mid:
                sidecar.pop(mid, None)

        logger.info(
            "SnipLayer.apply 删除 %d 条消息，释放 %d tokens", len(target), freed
        )
        return CompactionResult(
            success=True,
            layer="L2",
            saved_tokens=freed,
            messages_after=sim,
            details={"removed_count": len(target)},
        )


# ---------------------------------------------------------------------------
# L3 TimeDecayLayer
# ---------------------------------------------------------------------------

class TimeDecayLayer:
    """L3：时间衰减。

    早期 tool 消息 content 替换占位符；
    早期 assistant 消息（其全部 tool_calls 都已不在 keep_set）剥 reasoning_content。
    """

    def should_trigger(
        self,
        messages: List[Dict[str, Any]],
        sidecar: Dict[str, MessageMeta],
        current_tokens: int,
        max_allowed: int,
    ) -> bool:
        # proactive：token 已超 60% 阈值
        if max_allowed > 0 and current_tokens >= max_allowed * TIME_DECAY_PROACTIVE_RATIO:
            return True
        # reactive：距上次活动 > 60 分钟（基于 sidecar 时间戳）
        latest_ts = self._latest_activity(messages, sidecar)
        if latest_ts is None:
            return False
        gap_minutes = (datetime.now() - latest_ts).total_seconds() / 60.0
        return gap_minutes > GAP_THRESHOLD_MINUTES

    @staticmethod
    def _latest_activity(
        messages: List[Dict[str, Any]],
        sidecar: Dict[str, MessageMeta],
    ) -> "datetime | None":
        latest = None
        for m in messages:
            mid = m.get("_meta_id")
            if not mid or mid not in sidecar:
                continue
            ts = sidecar[mid].last_seen_at
            if latest is None or ts > latest:
                latest = ts
        return latest

    def apply(
        self,
        messages: List[Dict[str, Any]],
        sidecar: Dict[str, MessageMeta],
        token_counter: TokenCounter,
    ) -> CompactionResult:
        tool_ids = _collect_compactable_tool_call_ids(messages)
        if not tool_ids:
            return CompactionResult(
                skipped=True, layer="L3", reason="无可压缩工具调用"
            )

        keep_set = set(tool_ids[-KEEP_RECENT_TOOL_RESULTS:])
        new_messages: List[Dict[str, Any]] = []
        freed = 0
        cleared_tools = 0
        stripped_assts = 0

        for m in messages:
            new_m = self._compact_one(
                m, messages, keep_set, token_counter,
            )
            if new_m is m:
                new_messages.append(m)
                continue

            old_tokens = token_counter.count_message(m)
            new_tokens = token_counter.count_message(new_m)
            freed += old_tokens - new_tokens
            if m.get("role") == "tool":
                cleared_tools += 1
            else:
                stripped_assts += 1
            new_messages.append(new_m)

        if not _validate_pairs(new_messages):
            return CompactionResult(
                success=False,
                layer="L3",
                reason="L3 后工具对失效，回滚",
            )

        logger.info(
            "TimeDecayLayer.apply 清理 tool=%d 剥 reasoning=%d 释放 %d tokens",
            cleared_tools, stripped_assts, freed,
        )
        return CompactionResult(
            success=True,
            layer="L3",
            saved_tokens=freed,
            messages_after=new_messages,
            details={
                "cleared_tools": cleared_tools,
                "stripped_reasonings": stripped_assts,
            },
        )

    @staticmethod
    def _compact_one(
        m: Dict[str, Any],
        all_messages: List[Dict[str, Any]],
        keep_set: set,
        token_counter: TokenCounter,
    ) -> Dict[str, Any]:
        """对单条消息执行衰减；返回新对象表示已修改，返回原对象表示跳过。"""
        role = m.get("role")
        # —— 早期 tool 消息：content 替换占位符（保留 role + tool_call_id）——
        if role == "tool":
            content = m.get("content", "") or ""
            if content.startswith(L3_PLACEHOLDER_PREFIX):
                return m  # 幂等：已是占位符
            tcid = m.get("tool_call_id")
            if not tcid or tcid in keep_set:
                return m
            tool_name = _lookup_tool_name_by_call_id(all_messages, tcid)
            new_m = dict(m)
            new_m["content"] = (
                f"{L3_PLACEHOLDER_PREFIX} | tool={tool_name} | "
                f"tool_call_id={tcid} | 原文已不可见 | 可参考 large_results 归档]"
            )
            return new_m

        # —— 早期 assistant 消息：剥 reasoning_content ——
        if role == "assistant" and m.get("reasoning_content") and m.get("tool_calls"):
            tool_calls = m.get("tool_calls") or []
            if tool_calls and all(tc.get("id") not in keep_set for tc in tool_calls):
                new_m = {k: v for k, v in m.items() if k != "reasoning_content"}
                return new_m

        return m
