"""
循环引擎消息类型定义（纯类型层，零行为）。

对应 adopt-code 中的 SDKMessage / NormalizedMessage 体系，但裁剪到 Harness-Lite 的最小集：
- AssistantMessage / UserMessage / SystemMessage / ToolMessage：进入 transcript 的常规消息
- AttachmentMessage：注入到下一轮 LLM 输入的附件（如 prefetch 结果、max_turns 提示）
- TombstoneMessage：控制信号（流式 fallback 时清空已收消息），不进 transcript
- StreamEvent：流式事件（message_start / delta / stop / api_error），仅供 usage 统计

设计原则：
1. 严格 type hint；所有可空字段显式 Optional
2. dataclass + frozen=False（State 重建时构造新实例即可，无需就地修改）
3. 提供 `to_openai_dict()` 转换为 OpenAI API 所需的 dict 格式（兼容现有 engine.py 调用栈）
4. 提供 `from_openai_dict()` 反向工厂（用于从历史 JSON 加载）
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional, Union


# ============================================================
# 1. 进入 transcript 的常规消息（assistant / user / system / tool）
# ============================================================

@dataclass
class AssistantMessage:
    """LLM 输出消息。content 可为空（仅 tool_calls 的情况）。"""
    content: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    reasoning_content: Optional[str] = None  # thinking_mode 下的思维链
    # 标记该消息是否仅为 meta 信息（如 length 恢复 nudge），不进入持久化 transcript
    is_meta: bool = False

    def to_openai_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.is_meta:
            d["is_meta"] = True
        return d


@dataclass
class UserMessage:
    """用户消息（含真人输入、续写 nudge、attachment 注入等）。"""
    content: str
    is_meta: bool = False  # length 恢复 nudge 用 True，build_hot_swapped_context 会过滤

    def to_openai_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": "user", "content": self.content}
        if self.is_meta:
            d["is_meta"] = True
        return d


@dataclass
class SystemMessage:
    """系统消息（system prompt / compact boundary / api_error 等）。

    subtype 用于区分语义：
    - "prompt"          → 标准 system prompt
    - "compact_boundary"→ 压缩后的边界摘要
    - "api_error"       → LLM API 错误占位
    - "error_during_execution" → terminal=model_error 时的错误描述
    """
    content: str
    subtype: Literal["prompt", "compact_boundary", "api_error", "error_during_execution"] = "prompt"
    is_meta: bool = False

    def to_openai_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": "system", "content": self.content}
        if self.is_meta:
            d["is_meta"] = True
        return d


@dataclass
class ToolMessage:
    """工具执行结果消息（含 synthetic tool_result 占位）。"""
    tool_call_id: str
    content: str
    # 标记是否为 abort 路径补的 synthetic 占位（用于 OpenAI 闭合）
    is_synthetic: bool = False

    def to_openai_dict(self) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


# ============================================================
# 2. 控制信号 / 附件类消息（不一定进 transcript）
# ============================================================

@dataclass
class AttachmentMessage:
    """附件消息：注入到下一轮 LLM 输入或作为 SDK 事件吐出。

    subtype:
    - "skill_prefetch"   → skill 预取结果，注入 system
    - "memory_prefetch"  → mem0/markdown 长记忆注入
    - "max_turns_reached"→ 达到 max_turns 时的提示
    - "hook_stopped"     → stop hook 阻止继续的占位
    """
    subtype: str
    content: str
    # 是否需要拼到下一轮 messages（False 表示仅 SDK 事件）
    inject_to_next_turn: bool = False


@dataclass
class TombstoneMessage:
    """墓碑信号：流式 fallback 时通知消费方清空已收消息。不进 transcript。"""
    reason: str = "stream_fallback"


# ============================================================
# 3. 流式事件（仅供 usage 统计 / 进度展示）
# ============================================================

@dataclass
class StreamEvent:
    """流式事件。type ∈ {message_start, message_delta, message_stop, api_error}。"""
    type: Literal["message_start", "message_delta", "message_stop", "api_error"]
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolUseSummary:
    """工具调用摘要（由 haiku/小模型异步生成；一期可不实现，留类型）。"""
    summary: str
    related_tool_call_ids: List[str] = field(default_factory=list)


# ============================================================
# 4. 联合类型 + dict 互转
# ============================================================

LoopMessage = Union[
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ToolMessage,
    AttachmentMessage,
    TombstoneMessage,
    StreamEvent,
    ToolUseSummary,
]
"""所有循环引擎可能 yield 的消息联合类型。"""


# 进入 transcript / OpenAI API 的消息类型（不含控制信号 / 事件）
TranscriptMessage = Union[AssistantMessage, UserMessage, SystemMessage, ToolMessage]


def to_openai_dict_list(messages: List[TranscriptMessage]) -> List[Dict[str, Any]]:
    """批量转换为 OpenAI API 所需的 dict 列表（兼容现有 engine.call_llm_async）。"""
    return [m.to_openai_dict() for m in messages]


def from_openai_dict(d: Dict[str, Any]) -> TranscriptMessage:
    """从 OpenAI/历史 JSON 的 dict 反向构造（用于 memory.load_context 反序列化）。"""
    role = d.get("role")
    is_meta = bool(d.get("is_meta", False))
    if role == "assistant":
        return AssistantMessage(
            content=d.get("content", "") or "",
            tool_calls=d.get("tool_calls"),
            reasoning_content=d.get("reasoning_content"),
            is_meta=is_meta,
        )
    if role == "user":
        return UserMessage(content=d.get("content", "") or "", is_meta=is_meta)
    if role == "system":
        return SystemMessage(content=d.get("content", "") or "", is_meta=is_meta)
    if role == "tool":
        return ToolMessage(
            tool_call_id=d.get("tool_call_id", ""),
            content=d.get("content", "") or "",
        )
    raise ValueError(f"Unknown OpenAI message role: {role!r}")


__all__ = [
    "AssistantMessage",
    "UserMessage",
    "SystemMessage",
    "ToolMessage",
    "AttachmentMessage",
    "TombstoneMessage",
    "StreamEvent",
    "ToolUseSummary",
    "LoopMessage",
    "TranscriptMessage",
    "to_openai_dict_list",
    "from_openai_dict",
]
