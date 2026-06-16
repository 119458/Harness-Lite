"""
compact 包的共享类型与 token 计数器。

本模块为整个 compact 子包提供基础数据结构和 token 计算工具，
不依赖本包其他模块，可被 storage / local_layers / prompts / pipeline 自由引用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import tiktoken
except ModuleNotFoundError:
    tiktoken = None

logger = logging.getLogger("harness_lite.compact")


@dataclass
class MessageMeta:
    """sidecar 字典的 value：保存与单条消息绑定的元数据。

    通过消息 dict 上的 `_meta_id` 字段反向关联到本结构，
    避免直接污染消息 schema。
    """

    created_at: datetime
    last_seen_at: datetime
    large_ref_id: Optional[str] = None  # 若该消息已被 L1 落盘，记录 ref_id
    source_layer: Optional[str] = None  # 调试用：哪一层最后操作过


@dataclass
class ToolResultRef:
    """L1 落盘记录：磁盘文件元信息。"""

    ref_id: str
    tool_call_id: str
    tool_name: str
    disk_path: str
    content_hash: str
    byte_size: int
    preview: str


@dataclass
class CompactionResult:
    """每层 apply() 的统一返回类型。

    `messages_after=None` 表示未对消息列表做改动（caller 应保留原列表）；
    `success=False` 表示该层应当被回滚或视为失败。
    """

    success: bool = True
    skipped: bool = False
    layer: str = ""  # "L1"/"L2"/"L3"/"L5"/"L5-degraded"
    saved_tokens: int = 0
    messages_after: Optional[List[Dict[str, Any]]] = None  # None 表示不变
    reason: str = ""  # skipped / failed 时的解释
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.success and self.skipped:
            raise ValueError("CompactionResult: success=False 与 skipped=True 互斥")


@dataclass
class LayerStats:
    """status_callback 友好的累计统计（pipeline 持有，给 CLI 看）。"""

    l1_offload_count: int = 0
    l1_bytes_offloaded: int = 0
    l2_snip_freed_tokens: int = 0
    l3_apply_count: int = 0
    l3_total_tokens_freed: int = 0
    l5_apply_count: int = 0
    l5_total_tokens_freed: int = 0
    l5_consecutive_failures: int = 0


class TokenCounter:
    """封装 tiktoken cl100k_base，复用 manager.py 已有逻辑。

    单条消息的 token 计算与 `DynamicContextManager.calculate_messages_tokens`
    保持像素级对齐，便于在 pipeline 中替换而不影响阈值判定。
    """

    def __init__(self, model_name: str = "gpt-4-mini"):
        self.model_name = model_name
        self.encoder = None
        if tiktoken:
            try:
                self.encoder = tiktoken.encoding_for_model(self.model_name)
            except KeyError:
                try:
                    self.encoder = tiktoken.get_encoding("cl100k_base")
                except Exception:  # pragma: no cover - 编码器加载兜底
                    self.encoder = None

    def count_string(self, text: str) -> int:
        """计算单段字符串的 token 数；编码器不可用时退化为 len/3+1。"""
        if not text:
            return 0
        if self.encoder:
            return len(self.encoder.encode(text, disallowed_special=()))
        return len(text) // 3 + 1

    def count_message(self, msg: Dict[str, Any]) -> int:
        """单条消息的 token 数（与现有 manager.py:54-77 对齐）。"""
        total = 3
        total += self.count_string(msg.get("content", "") or "")
        total += self.count_string(msg.get("role", "") or "")
        total += self.count_string(msg.get("name", "") or "")
        if msg.get("reasoning_content"):
            total += self.count_string(msg["reasoning_content"])
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += 10
            for tc in tool_calls:
                total += self.count_string(tc.get("id", "") or "")
                func = tc.get("function", {}) or {}
                total += self.count_string(func.get("name", "") or "")
                total += self.count_string(func.get("arguments", "") or "")
        return total

    def count_messages(self, messages: List[Dict[str, Any]]) -> int:
        """整个消息堆栈的 token 总数，含 OpenAI 末尾 priming。"""
        total = sum(self.count_message(m) for m in messages)
        total += 3  # OpenAI 末尾 priming token
        return total
