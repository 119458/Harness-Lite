"""
Hook 框架（hooks.py）。

对应 master.md 阶段 D / 4.4 不变量 #11：
- 每个 hook `asyncio.wait_for(timeout=N)` 隔离
- 单 hook 异常 → 记录 + 跳过（不破坏链）
- stop_hook 异常 → 默认 fail-open 放行

两种 Hook 类型：
1. PostSamplingHook —— LLM 响应后、工具执行前调用（可用于审计/日志/变换）
2. StopHook —— 决定是否应终止当前循环（可用于安全策略熔断）
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("harness_lite.hooks")


# ============================================================
# Hook 抽象基类
# ============================================================

class PostSamplingHook(ABC):
    """LLM 采样后钩子：在工具执行前调用。"""

    @abstractmethod
    async def on_sampling(self, messages: List[Dict], response: Dict, session_id: str) -> None:
        """
        处理 LLM 响应。

        Args:
            messages: 当前消息列表。
            response: LLM 原始响应 dict。
            session_id: 当前会话 ID。
        """
        ...


class StopHook(ABC):
    """停止钩子：决定是否应终止当前循环。"""

    @abstractmethod
    async def should_stop(self, messages: List[Dict], response: Dict, session_id: str) -> bool:
        """
        判定是否应停止。

        Returns:
            True = 阻止继续（终止循环），False = 放行。
        """
        ...


# ============================================================
# Hook 注册与执行器
# ============================================================

@dataclass
class HookRegistry:
    """Hook 注册表。"""

    post_sampling_hooks: List[PostSamplingHook] = field(default_factory=list)
    stop_hooks: List[StopHook] = field(default_factory=list)

    def register_post_sampling(self, hook: PostSamplingHook) -> None:
        self.post_sampling_hooks.append(hook)

    def register_stop(self, hook: StopHook) -> None:
        self.stop_hooks.append(hook)


_default_timeout: float = 10.0


async def run_post_sampling_hooks(
    hooks: List[PostSamplingHook],
    messages: List[Dict],
    response: Dict,
    session_id: str,
    timeout: float = _default_timeout,
) -> None:
    """
    串行执行所有 PostSamplingHook。

    单 hook 异常 → log + skip（不破坏链）。
    每个 hook 受 asyncio.wait_for 超时保护。
    """
    for hook in hooks:
        try:
            await asyncio.wait_for(
                hook.on_sampling(messages, response, session_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "PostSamplingHook %s timed out after %.1fs",
                type(hook).__name__, timeout,
            )
        except asyncio.CancelledError:
            raise  # 必须冒泡
        except Exception as exc:
            logger.exception(
                "PostSamplingHook %s failed: %s", type(hook).__name__, exc,
            )


async def run_stop_hooks(
    hooks: List[StopHook],
    messages: List[Dict],
    response: Dict,
    session_id: str,
    timeout: float = _default_timeout,
) -> bool:
    """
    执行所有 StopHook，任一返回 True 则终止。

    fail-open 原则：异常 → 记录 + 返回 False（放行，不阻塞执行）。
    每个 hook 受 asyncio.wait_for 超时保护。
    """
    for hook in hooks:
        try:
            should = await asyncio.wait_for(
                hook.should_stop(messages, response, session_id),
                timeout=timeout,
            )
            if should:
                logger.info("StopHook %s triggered stop", type(hook).__name__)
                return True
        except asyncio.TimeoutError:
            logger.warning(
                "StopHook %s timed out after %.1fs, fail-open",
                type(hook).__name__, timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "StopHook %s failed: %s, fail-open", type(hook).__name__, exc,
            )
    return False


# ============================================================
# 内置 Hook 实现（一期）
# ============================================================

class LengthRecoveryStopHook(StopHook):
    """
    当 finish_reason == "length" 且恢复次数耗尽时终止。

    一期：实际逻辑在 recovery.py / strategy 中，此处作为预留 hook 占位。
    """

    async def should_stop(self, messages: List[Dict], response: Dict, session_id: str) -> bool:
        return False  # 一期不在此处判断


class ToolErrorFuseStopHook(StopHook):
    """
    工具连续异常熔断。

    一期：实际逻辑在 strategy._stage_3_tool_orchestration 中，此处作为预留占位。
    """

    async def should_stop(self, messages: List[Dict], response: Dict, session_id: str) -> bool:
        return False  # 一期不在此处判断


# ============================================================
# 全局注册表
# ============================================================

hook_registry = HookRegistry()

hook_registry.register_stop(LengthRecoveryStopHook())
hook_registry.register_stop(ToolErrorFuseStopHook())


__all__ = [
    "PostSamplingHook",
    "StopHook",
    "HookRegistry",
    "hook_registry",
    "run_post_sampling_hooks",
    "run_stop_hooks",
    "LengthRecoveryStopHook",
    "ToolErrorFuseStopHook",
]