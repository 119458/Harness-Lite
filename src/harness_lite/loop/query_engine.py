"""
L1 会话编排层（QueryEngine）。

对应 adopt-code/QueryEngine.ts，职责：
- 一个 conversation 对应一个 QueryEngine 实例（但 B2 阶段由 AsyncLoopEngine 每 turn 创建）
- submit_message() 是 AsyncGenerator，yield LoopMessage 供消费方处理
- 管理 mutableMessages、abort 信号、usage 统计
- 组装 system prompt + user input + history → 调用 L2 循环引擎

B2 阶段实现策略：
- QueryEngine 持有 AsyncLoopEngine 引用，复用其所有底层能力
- submit_message 内部仍委托 ReActStrategy.execute，但把结果包装为 LoopMessage yield 出来
- engine.run() 外层消费 submit_message 生成器，fan-out 到 stream/status callback 并最终返回 str
- 这样保留了 engine.run() -> str 的外部契约，同时为 C 阶段（strategy 改为 generator）铺路
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, AsyncGenerator

from harness_lite.loop.messages import (
    AssistantMessage,
    AttachmentMessage,
    LoopMessage,
    StreamEvent,
    SystemMessage,
    TombstoneMessage,
    ToolMessage,
    UserMessage,
    from_openai_dict,
    to_openai_dict_list,
)
from harness_lite.loop.terminal import Terminal
from harness_lite.config.loader import get_llm_config

logger = logging.getLogger("harness_lite.query_engine")


@dataclass
class TurnResult:
    """单 turn 的最终结果（由 engine.run 消费）。"""
    text: str
    terminal: Terminal = Terminal.COMPLETED
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class QueryEngine:
    """
    L1 会话编排层 —— 管理 turn 生命周期与消息流。

    设计约束（master.md 3.5 节）：
    - 不改变 engine.run() -> str 签名
    - current_session_id.set() 在 turn 入口执行（engine.run 负责）
    - save_context 每 turn 仅一次（strategy._stage_4_state_persistence 负责）
    - is_meta 消息由 build_hot_swapped_context 过滤
    """

    def __init__(
        self,
        engine: Any,  # AsyncLoopEngine 引用
        session_id: str,
    ):
        self._engine = engine
        self._session_id = session_id

        # 会话级可变状态
        self._mutable_messages: List[Dict[str, Any]] = []
        self._abort_event: asyncio.Event = asyncio.Event()
        self._permission_denials: List[Dict[str, Any]] = []

        # usage 统计（一期暂不精确计算，预留位）
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_aborted(self) -> bool:
        return self._abort_event.is_set()

    def abort(self) -> None:
        """外部中断信号（Ctrl+C / SDK abort）。"""
        self._abort_event.set()

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取当前会话全部消息（供 engine.memory 等外部模块读取）。"""
        return list(self._mutable_messages)

    # ================================================================
    # 核心：submit_message —— 单 turn 消息流
    # ================================================================

    async def submit_message(
        self,
        prompt: str,
        *,
        stream_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> AsyncGenerator[LoopMessage, None]:
        """
        提交一条用户消息，启动一个 turn 的处理循环。

        C 阶段：直接消费 strategy.execute_stream，逐条转发 LoopMessage。
        """
        # 1. 构建 context（委托 engine）
        messages = self._engine.build_hot_swapped_context(prompt, self._session_id)
        self._mutable_messages = list(messages)

        # 2. abort 早期检查
        if self.is_aborted:
            yield SystemMessage(content="会话已被中断", subtype="error_during_execution")
            return

        # 3. 消费 strategy.execute_stream
        try:
            async for msg in self._engine.strategy.execute_stream(
                task=prompt,
                engine=self._engine,
                session_id=self._session_id,
                stream_callback=stream_callback,
                status_callback=status_callback,
            ):
                # 同步刷新 mutable_messages（C 阶段轻量化：只在 yield AssistantMessage 时更新）
                if isinstance(msg, AssistantMessage):
                    # strategy 内部已 save_context；从 memory 加载最新状态
                    try:
                        self._mutable_messages = self._engine.memory.load_context(self._session_id)
                    except Exception as e:
                        logger.warning(f"failed to refresh mutable_messages: {e}")

                yield msg

        except asyncio.CancelledError:
            yield SystemMessage(content="会话被用户中断", subtype="error_during_execution")
            raise
        except KeyboardInterrupt:
            yield SystemMessage(content="人工审计中断", subtype="error_during_execution")
            raise
        except Exception as e:
            logger.exception("Unhandled exception in submit_message")
            yield SystemMessage(
                content=f"执行过程中发生错误: {str(e)}",
                subtype="error_during_execution",
            )

    # ================================================================
    # 辅助方法
    # ================================================================

    def _consume_to_result(
        self,
        stream_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """
        返回一个异步函数，消费 submit_message 生成器并返回 TurnResult。
        供 engine.run() 内部使用。
        """
        async def _run(prompt: str) -> TurnResult:
            final_text = ""
            terminal = Terminal.COMPLETED

            async for msg in self.submit_message(
                prompt,
                stream_callback=stream_callback,
                status_callback=status_callback,
            ):
                if isinstance(msg, AssistantMessage):
                    final_text = msg.content
                elif isinstance(msg, SystemMessage) and msg.subtype == "error_during_execution":
                    final_text = msg.content
                    terminal = Terminal.MODEL_ERROR

            return TurnResult(
                text=final_text,
                terminal=terminal,
                total_input_tokens=self._total_input_tokens,
                total_output_tokens=self._total_output_tokens,
            )

        return _run


__all__ = ["QueryEngine", "TurnResult"]
