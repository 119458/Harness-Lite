"""ReActStrategy 与长期记忆筛选的并行调度单元测试。

覆盖：
- 有 tool_calls 时：process_tool_calls_async 与 async_filter_recommendations 同时启动
- 工具结果 append 完成后，将筛选结果追加为 `{"role":"system", is_meta=True}`
- 筛选返回空时不 append meta system
- 工具刚 read_file 读过的记忆（mark_read 后）会被 read_set 后置过滤掉
- 召回失败（async 返回 []）不影响工具执行
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from harness_lite.loop.strategy import ReActStrategy
from harness_lite.memory.long_term import MemoryHeader


# ---------------------------------------------------------------------------
# Fake long-term 与 fake engine 辅助
# ---------------------------------------------------------------------------

class _FakeLongTerm:
    """模拟 LongTermMemoryManager 的最小接口，记录调用情况。"""

    def __init__(
        self,
        recall_result: Optional[List[MemoryHeader]] = None,
        read_set: Optional[set] = None,
        raise_in_recall: bool = False,
    ):
        self._recall_result = recall_result or []
        self._read_set = read_set or set()
        self._raise = raise_in_recall
        self.async_calls: List[Dict[str, Any]] = []

    async def async_filter_recommendations(
        self, query: str, session_id: str = "default",
        recent_tools: Optional[List[str]] = None,
    ) -> List[MemoryHeader]:
        self.async_calls.append({
            "query": query,
            "session_id": session_id,
            "recent_tools": list(recent_tools or []),
        })
        if self._raise:
            return []
        return list(self._recall_result)

    def get_read_set(self, session_id: str) -> set:
        return set(self._read_set)

    def build_recommendation_section(self, headers: List[MemoryHeader]) -> str:
        if not headers:
            return ""
        lines = ["## 本轮可能相关的长期记忆（推荐，仅供参考）"]
        for h in headers:
            lines.append(f"- {h.filename} — {h.description or ''}")
        return "\n".join(lines)


class _FakeMemory:
    def __init__(self, long_term: _FakeLongTerm):
        self.long_term = long_term


class _FakePipeline:
    def record_tool_result(self, msg, session_id):
        return msg


class _FakeContextManager:
    pipeline = _FakePipeline()


class _FakeEngine:
    """模拟 strategy 真正用到的 engine 接口。"""

    def __init__(
        self,
        long_term: _FakeLongTerm,
        history_tools: Optional[List[str]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
    ):
        self.memory = _FakeMemory(long_term)
        self._history_tools = history_tools or []
        self._tool_results = tool_results or []
        self.process_tool_calls_called_with: Optional[List[Dict]] = None

    def _collect_recent_tools(self, session_id: str) -> List[str]:
        return list(self._history_tools)

    async def process_tool_calls_async(
        self, tool_calls: List[Dict], session_id: str,
    ) -> List[Dict[str, Any]]:
        self.process_tool_calls_called_with = tool_calls
        return list(self._tool_results)


def _make_strategy() -> ReActStrategy:
    """构造一个 strategy，但避免触碰真正的 context_manager.pipeline 业务。"""
    s = ReActStrategy()
    s.context_manager = _FakeContextManager()  # type: ignore[assignment]
    return s


def _tool_call(name: str, call_id: str = "c1") -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _header(filename: str, description: str = "desc") -> MemoryHeader:
    return MemoryHeader(
        filename=filename, file_path=f"/tmp/{filename}", mtime_ms=0,
        description=description, type="user", name=filename.split(".")[0],
    )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_tool_call_branch_launches_recall_and_appends_meta():
    """有 tool_calls 时：召回与工具执行都启动，工具结果后追加 is_meta system 推荐。"""
    long_term = _FakeLongTerm(recall_result=[_header("user-x.md", "desc x")])
    tool_results = [{"tool_call_id": "c1", "output": "tool ok"}]
    engine = _FakeEngine(long_term=long_term, tool_results=tool_results)
    strategy = _make_strategy()

    messages: List[Dict[str, Any]] = [{"role": "user", "content": "请帮我查记忆"}]
    tool_calls = [_tool_call("read_file", "c1")]

    out_messages, has_error = asyncio.run(strategy._stage_3_tool_orchestration(
        messages=messages,
        tool_calls=tool_calls,
        engine=engine,
        session_id="sid",
        assistant_content="我需要先读个文件",
        assistant_message={"content": "我需要先读个文件"},
        status_callback=None,
    ))

    # 工具执行被调用
    assert engine.process_tool_calls_called_with == tool_calls
    # 召回被调用，且包含当前工具名
    assert len(long_term.async_calls) == 1
    assert "read_file" in long_term.async_calls[0]["recent_tools"]
    # 顺序：assistant -> tool -> system(is_meta)
    roles = [m.get("role") for m in out_messages]
    assert roles[-3] == "assistant"
    assert roles[-2] == "tool"
    assert roles[-1] == "system"
    assert out_messages[-1].get("is_meta") is True
    assert "user-x.md" in out_messages[-1]["content"]
    assert has_error is False


def test_no_recall_results_skips_meta_append():
    """筛选返回空时不 append meta system 消息。"""
    long_term = _FakeLongTerm(recall_result=[])
    tool_results = [{"tool_call_id": "c1", "output": "ok"}]
    engine = _FakeEngine(long_term=long_term, tool_results=tool_results)
    strategy = _make_strategy()

    messages: List[Dict[str, Any]] = [{"role": "user", "content": "去执行"}]
    tool_calls = [_tool_call("read_file", "c1")]

    out_messages, _ = asyncio.run(strategy._stage_3_tool_orchestration(
        messages=messages,
        tool_calls=tool_calls,
        engine=engine,
        session_id="sid",
        assistant_content="开干",
        assistant_message={"content": "开干"},
        status_callback=None,
    ))

    # 最后一条不应是 is_meta system
    assert not any(m.get("is_meta") for m in out_messages)
    # 最后一条应是 tool 结果
    assert out_messages[-1].get("role") == "tool"


def test_read_set_post_filter_drops_recently_read_memory():
    """工具执行时若 read_file 刚读过某记忆（mark_read），推荐里不应再出现。"""
    # 召回返回 a/b 两条，但工具结束后 read_set 已含 a，则 a 被剔除
    long_term = _FakeLongTerm(
        recall_result=[_header("a.md"), _header("b.md")],
        read_set={"a.md"},
    )
    tool_results = [{"tool_call_id": "c1", "output": "ok"}]
    engine = _FakeEngine(long_term=long_term, tool_results=tool_results)
    strategy = _make_strategy()

    messages: List[Dict[str, Any]] = [{"role": "user", "content": "q"}]
    tool_calls = [_tool_call("read_file", "c1")]

    out_messages, _ = asyncio.run(strategy._stage_3_tool_orchestration(
        messages=messages,
        tool_calls=tool_calls,
        engine=engine,
        session_id="sid",
        assistant_content="",
        assistant_message={},
        status_callback=None,
    ))

    meta = [m for m in out_messages if m.get("is_meta")]
    assert len(meta) == 1
    content = meta[0]["content"]
    assert "b.md" in content
    assert "a.md" not in content


def test_recall_failure_does_not_break_tool_execution(monkeypatch):
    """召回内部异常被 async 包装吞成 []，主流程仍能正常 append tool messages。"""
    long_term = _FakeLongTerm(recall_result=[], raise_in_recall=True)
    tool_results = [{"tool_call_id": "c1", "output": "tool result"}]
    engine = _FakeEngine(long_term=long_term, tool_results=tool_results)
    strategy = _make_strategy()

    messages: List[Dict[str, Any]] = [{"role": "user", "content": "q"}]
    tool_calls = [_tool_call("read_file", "c1")]

    out_messages, has_error = asyncio.run(strategy._stage_3_tool_orchestration(
        messages=messages,
        tool_calls=tool_calls,
        engine=engine,
        session_id="sid",
        assistant_content="",
        assistant_message={},
        status_callback=None,
    ))

    # 工具结果照常 append
    assert any(m.get("role") == "tool" for m in out_messages)
    # 推荐为空 -> 不 append meta
    assert not any(m.get("is_meta") for m in out_messages)
    assert has_error is False


def test_build_memory_recall_query_combines_user_and_assistant_and_tools():
    """query 应包含最近一条 user 消息、当前推理片段、本轮工具名。"""
    messages = [
        {"role": "user", "content": "请读文件 A"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "再读文件 B"},
    ]
    q = ReActStrategy._build_memory_recall_query(
        messages, assistant_content="先调用 read_file", tool_names=["read_file"],
    )
    assert "再读文件 B" in q
    assert "先调用 read_file" in q
    assert "read_file" in q
