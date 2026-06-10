"""
错误恢复策略（recovery.py）。

对应 master.md 第七节「异常分类清单」与 4.4 不变量 #9（RecoveryBudget 硬上限）。

设计原则：
1. 集中管理所有恢复计数器（max_output_tokens / reactive_compact / fallback_model 等）
2. 严格 narrow catch —— 每类异常对应一种恢复策略，不允许裸 except Exception
3. 计数器超限即转 Terminal.MODEL_ERROR 或 Terminal.PROMPT_TOO_LONG，禁止死循环
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from harness_lite.loop.terminal import Terminal

logger = logging.getLogger("harness_lite.recovery")


# ============================================================
# 1. RecoveryBudget —— 跨迭代保留的恢复预算
# ============================================================

@dataclass
class RecoveryBudget:
    """集中管理所有恢复计数器。每个 turn 起始时新建，跨迭代复用。"""

    # 输出截断恢复：≤3 次（对应原 strategy.MAX_OUTPUT_TOKENS_RECOVERY_LIMIT）
    max_output_tokens_recovery_count: int = 0
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT: int = field(default=3, repr=False)

    # reactive compact：一次性（防止死循环）
    has_attempted_reactive_compact: bool = False

    # fallback model：一次性（一期暂不实现跨厂商 fallback，预留位）
    has_attempted_fallback_model: bool = False

    # 工具连续异常熔断：≥3 次熔断（对应原 strategy.consecutive_errors >= 3）
    consecutive_tool_errors: int = 0
    CONSECUTIVE_TOOL_ERROR_LIMIT: int = field(default=3, repr=False)

    def can_recover_length(self) -> bool:
        """是否还能再尝试 length 恢复。"""
        return self.max_output_tokens_recovery_count < self.MAX_OUTPUT_TOKENS_RECOVERY_LIMIT

    def consume_length_recovery(self) -> int:
        """消费一次 length 恢复额度，返回当前是第几次。"""
        self.max_output_tokens_recovery_count += 1
        return self.max_output_tokens_recovery_count

    def can_reactive_compact(self) -> bool:
        return not self.has_attempted_reactive_compact

    def mark_reactive_compact_attempted(self) -> None:
        self.has_attempted_reactive_compact = True

    def record_tool_error(self) -> int:
        """工具异常 +1，返回当前连续异常数。"""
        self.consecutive_tool_errors += 1
        return self.consecutive_tool_errors

    def reset_tool_errors(self) -> None:
        self.consecutive_tool_errors = 0

    def is_tool_error_fused(self) -> bool:
        return self.consecutive_tool_errors >= self.CONSECUTIVE_TOOL_ERROR_LIMIT


# ============================================================
# 2. 异常分类与恢复决策
# ============================================================

class RecoveryAction(str, Enum):
    """单次恢复尝试的决策结果。"""

    # 注入 nudge 消息后继续主循环
    INJECT_LENGTH_NUDGE = "inject_length_nudge"

    # 触发 reactive compact 后重试
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"

    # 退避后重试（仅限网络/限流类）
    BACKOFF_RETRY = "backoff_retry"

    # 直接终止（携带 Terminal）
    TERMINATE = "terminate"

    # 重抛异常（CancelledError 等必须冒泡的）
    RERAISE = "reraise"


@dataclass
class RecoveryDecision:
    """恢复决策详情。"""
    action: RecoveryAction
    terminal: Optional[Terminal] = None  # action=TERMINATE 时携带
    reason: str = ""
    backoff_seconds: float = 0.0


def classify_finish_reason(finish_reason: str, budget: RecoveryBudget) -> RecoveryDecision:
    """
    根据 finish_reason 字段判定恢复策略。

    对应 query.ts 中 isWithheldMaxOutputTokens / handleStopHooks 的入口。
    """
    if finish_reason == "length":
        if budget.can_recover_length():
            return RecoveryDecision(
                action=RecoveryAction.INJECT_LENGTH_NUDGE,
                reason=f"output truncated, attempting nudge (count={budget.max_output_tokens_recovery_count + 1})",
            )
        else:
            return RecoveryDecision(
                action=RecoveryAction.TERMINATE,
                terminal=Terminal.MODEL_ERROR,
                reason=f"length recovery exhausted (limit={budget.MAX_OUTPUT_TOKENS_RECOVERY_LIMIT})",
            )

    if finish_reason == "error":
        # _stream_llm_events 内部异常已转为 error finish_reason
        return RecoveryDecision(
            action=RecoveryAction.TERMINATE,
            terminal=Terminal.MODEL_ERROR,
            reason="stream returned error finish_reason",
        )

    # stop / tool_calls / content_filter 等正常 finish_reason 不进入恢复路径
    return RecoveryDecision(action=RecoveryAction.TERMINATE, terminal=Terminal.COMPLETED)


def classify_llm_exception(exc: BaseException, budget: RecoveryBudget) -> RecoveryDecision:
    """
    根据 LLM 调用过程抛出的异常判定恢复策略。

    覆盖 master.md 第七节异常清单中的 P0/P1 项目。

    注意：CancelledError / KeyboardInterrupt 必须重抛，由 query_engine 的 finally 兜底。
    """
    # ---- P0: 上下文超长 ----
    if _is_context_length_exceeded(exc):
        if budget.can_reactive_compact():
            return RecoveryDecision(
                action=RecoveryAction.REACTIVE_COMPACT_RETRY,
                reason="prompt too long, attempting reactive compact",
            )
        return RecoveryDecision(
            action=RecoveryAction.TERMINATE,
            terminal=Terminal.PROMPT_TOO_LONG,
            reason="reactive compact already attempted, terminating",
        )

    # ---- P0: 取消 ----
    import asyncio as _aio
    if isinstance(exc, _aio.CancelledError):
        return RecoveryDecision(
            action=RecoveryAction.RERAISE,
            reason="async task cancelled, must propagate",
        )
    if isinstance(exc, KeyboardInterrupt):
        return RecoveryDecision(
            action=RecoveryAction.RERAISE,
            reason="keyboard interrupt, must propagate",
        )

    # ---- P1: 网络瞬态 / 限流 / 5xx ----
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return RecoveryDecision(
                action=RecoveryAction.TERMINATE,
                terminal=Terminal.MODEL_ERROR,
                reason=f"rate limit (SDK retries exhausted): {exc}",
            )
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return RecoveryDecision(
                action=RecoveryAction.TERMINATE,
                terminal=Terminal.MODEL_ERROR,
                reason=f"network error (SDK retries exhausted): {exc}",
            )
        if isinstance(exc, openai.InternalServerError):
            return RecoveryDecision(
                action=RecoveryAction.TERMINATE,
                terminal=Terminal.MODEL_ERROR,
                reason=f"upstream 5xx (SDK retries exhausted): {exc}",
            )
        if isinstance(exc, openai.BadRequestError):
            # 非 context_length 的 400 错误，向上抛
            return RecoveryDecision(
                action=RecoveryAction.TERMINATE,
                terminal=Terminal.MODEL_ERROR,
                reason=f"bad request: {exc}",
            )
    except ImportError:
        pass

    # ---- 兜底：未分类异常 ----
    return RecoveryDecision(
        action=RecoveryAction.TERMINATE,
        terminal=Terminal.MODEL_ERROR,
        reason=f"unclassified exception ({type(exc).__name__}): {exc}",
    )


def _is_context_length_exceeded(exc: BaseException) -> bool:
    """检测是否为 prompt 超长错误（兼容多家厂商的错误形态）。"""
    try:
        import openai
        if not isinstance(exc, openai.BadRequestError):
            return False
    except ImportError:
        return False

    msg = str(exc).lower()
    keywords = [
        "context_length_exceeded",
        "context length exceeded",
        "prompt is too long",
        "maximum context length",
        "too many tokens",
        "context_length",
    ]
    return any(k in msg for k in keywords)


# ============================================================
# 3. nudge 消息构造（length 恢复用）
# ============================================================

def build_length_recovery_messages(
    assistant_content: str,
) -> List[Dict[str, Any]]:
    """
    构造 length 恢复用的两条注入消息（assistant 占位 + user nudge）。

    nudge 消息携带 is_meta=True，确保 build_hot_swapped_context 过滤掉，
    不进入持久化 transcript。
    """
    return [
        {
            "role": "assistant",
            "content": (assistant_content or "") + "\n...[由于输出字数限制，此处被系统安全截断]...",
        },
        {
            "role": "user",
            "content": "输出字数限制已达到。请从您中断思考的地方直接继续进行。无需道歉，也无需回顾您之前正在做的事情。继续进行。",
            "is_meta": True,
        },
    ]


__all__ = [
    "RecoveryBudget",
    "RecoveryAction",
    "RecoveryDecision",
    "classify_finish_reason",
    "classify_llm_exception",
    "build_length_recovery_messages",
]
