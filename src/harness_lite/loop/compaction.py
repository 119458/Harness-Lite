"""
多级上下文压缩管线（compaction.py）。

对应 master.md 阶段 D 需求：
- snip（最低级）：暴力裁切最旧消息
- micro（中级）：将单对 tool 交互摘要为 1 句
- collapse（高级）：将多对 tool 交互批量摘要
- auto（自动级）：基于阈值触发，默认接入 DynamicContextManager.compress_if_overflow

设计约束：
- 每级压缩返回 (messages, compacted_info) 二元组，compacted_info 包含字节/消息数释放统计
- auto 级与现有 DynamicContextManager 无缝衔接
- 所有压缩操作都是幂等的（compress(compress(m)) == compress(m) 不会连环压缩）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("harness_lite.compaction")


@dataclass
class CompactedInfo:
    """压缩结果统计信息。"""
    original_token_count: int = 0
    final_token_count: int = 0
    original_message_count: int = 0
    final_message_count: int = 0
    level: str = ""  # "snip" / "micro" / "collapse" / "auto"

    @property
    def saved_tokens(self) -> int:
        return self.original_token_count - self.final_token_count


# ================================================================
# Level 1: snip —— 暴力裁切
# ================================================================

def snip_compact(
    messages: List[Dict[str, Any]],
    *,
    anchor_count: int = 2,
    trim_ratio: float = 0.3,
) -> tuple[List[Dict[str, Any]], CompactedInfo]:
    """
    最低级压缩：保留锚点消息（system + 首个 user），裁切掉最早 trim_ratio 比例的历史。

    Args:
        messages: 完整消息列表。
        anchor_count: 保留的锚点消息数（默认 2 = system + first user）。
        trim_ratio: 从历史中裁切的比例（默认 0.3 = 裁掉 30%）。

    Returns:
        (压缩后的消息列表, 压缩统计信息)
    """
    info = CompactedInfo(
        original_message_count=len(messages),
        level="snip",
    )

    if len(messages) <= anchor_count + 2:
        info.final_message_count = len(messages)
        return messages, info

    anchor = messages[:anchor_count]
    history = messages[anchor_count:]

    trim_count = max(1, int(len(history) * trim_ratio))
    trimmed = history[trim_count:]

    result = anchor + trimmed
    info.final_message_count = len(result)
    return result, info


# ================================================================
# Level 2: micro —— 单对 tool 摘要
# ================================================================

def micro_compact(
    messages: List[Dict[str, Any]],
    *,
    anchor_count: int = 2,
) -> tuple[List[Dict[str, Any]], CompactedInfo]:
    """
    中级压缩：找到最早的一对 assistant(tool_call) + tool(response)，
    用一条 system 摘要消息替换。

    一期实现：stub —— 标记位置但不实际调用 LLM 摘要，降级为 snip。

    Args:
        messages: 完整消息列表。
        anchor_count: 保留的锚点消息数。

    Returns:
        (压缩后的消息列表, 压缩统计信息)
    """
    info = CompactedInfo(
        original_message_count=len(messages),
        level="micro",
    )

    if len(messages) <= anchor_count + 2:
        info.final_message_count = len(messages)
        return messages, info

    # ---- 一期降级：不实际调用 LLM，使用 snip 策略 ----
    logger.debug("micro_compact: stub mode, falling back to snip")
    return snip_compact(messages, anchor_count=anchor_count, trim_ratio=0.2)


# ================================================================
# Level 3: collapse —— 批量 tool 摘要
# ================================================================

async def collapse_compact(
    messages: List[Dict[str, Any]],
    *,
    anchor_count: int = 2,
    engine: Any = None,
) -> tuple[List[Dict[str, Any]], CompactedInfo]:
    """
    高级压缩：将多对 tool 交互批量提交 LLM 摘要，替换为一条归档消息。

    一期实现：stub —— NotImplementedError，提示尚未实现。

    Raises:
        NotImplementedError: 一期暂不实现。
    """
    raise NotImplementedError(
        "collapse_compact is not implemented in Phase D. "
        "Use auto_compact with DynamicContextManager instead."
    )


# ================================================================
# Level 4: auto —— 自动阈值压缩
# ================================================================

async def auto_compact(
    messages: List[Dict[str, Any]],
    *,
    engine: Any,
    session_id: str,
    current_cwd: str = "/",
    status_callback: Optional[Callable[[str], None]] = None,
    max_allowed_tokens: int = 64000,
) -> tuple[List[Dict[str, Any]], CompactedInfo]:
    """
    自动级压缩：基于 token 阈值决策，接入 DynamicContextManager.compress_if_overflow。

    这是默认推荐入口。与现有 _stage_1_context_optimization 行为一致。

    Args:
        messages: 完整消息列表。
        engine: AsyncLoopEngine 实例（context_manager 从 engine.strategy 获取）。
        session_id: 当前会话 ID。
        current_cwd: 当前 bash 工作目录（用于压缩锚点）。
        status_callback: 状态回调。
        max_allowed_tokens: Token 阈值（覆盖 context_manager 的默认值）。

    Returns:
        (压缩后的消息列表, 压缩统计信息)
    """
    from harness_lite.context.manager import DynamicContextManager

    info = CompactedInfo(
        original_message_count=len(messages),
        level="auto",
    )

    # 获取或创建 context_manager
    if hasattr(engine, "strategy") and hasattr(engine.strategy, "context_manager"):
        cm = engine.strategy.context_manager
    else:
        cm = DynamicContextManager(max_allowed_tokens=max_allowed_tokens)

    original_tokens = cm.calculate_messages_tokens(messages)
    info.original_token_count = original_tokens

    # 低于阈值不压缩
    if original_tokens <= cm.max_allowed_tokens:
        info.final_token_count = original_tokens
        info.final_message_count = len(messages)
        return messages, info

    compressed = await cm.compress_if_overflow(
        messages=messages,
        engine=engine,
        current_cwd=current_cwd,
        status_callback=status_callback,
    )

    info.final_token_count = cm.calculate_messages_tokens(compressed)
    info.final_message_count = len(compressed)
    return compressed, info


__all__ = ["CompactedInfo", "snip_compact", "micro_compact", "collapse_compact", "auto_compact"]