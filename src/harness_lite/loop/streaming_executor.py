"""
并行工具执行引擎（streaming_executor.py）。

对应 master.md 阶段 D 需求：
- asyncio.create_task 并发执行多个工具调用
- finally 中 task.cancel() + gather(return_exceptions=True) 回收
- asyncio.shield 保护安全审计 + 落盘关键区
- abort 时补 synthetic tool 消息（防止 OpenAI 400）

设计约束（master.md 3.5 / 4.4）：
- security.intercept 是同步方法，不能被并行化绕过 → 每个工具在各自 task 内串行调用
- Layer 3 _human_audit 用 input()，通过 asyncio.to_thread 桥接
- 单个工具异常不波及同批次其他工具（Fail-Fast 由 strategy 的 has_error 判定）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from harness_lite.loop.messages import ToolMessage

logger = logging.getLogger("harness_lite.streaming_executor")


class StreamingExecutor:
    """
    并行工具执行器。

    用法：
        executor = StreamingExecutor(engine, session_id)
        results, synthetic_messages = await executor.execute(tool_calls)
        # 若中途 abort，synthetic_messages 包含未完成工具的占位 ToolMessage
    """

    def __init__(self, engine: Any, session_id: str):
        self._engine = engine
        self._session_id = session_id

    async def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        abort_event: Optional[asyncio.Event] = None,
    ) -> tuple[List[Dict[str, Any]], List[ToolMessage]]:
        """
        并发执行所有工具调用。

        Args:
            tool_calls: OpenAI 格式的 tool_calls 列表。
            abort_event: 可选的 abort 信号，设置后取消未开始/进行中的任务。

        Returns:
            (results, synthetic_messages)
            - results: 与 process_tool_calls_async 格式一致的执行结果列表
            - synthetic_messages: 因 abort 未能执行完成的工具占位 ToolMessage 列表
        """
        valid_tool_calls = [
            tc for tc in tool_calls if tc.get("function", {}).get("name")
        ]
        if not valid_tool_calls:
            return [], []

        tasks: Dict[str, asyncio.Task] = {}
        results_map: Dict[str, Dict[str, Any]] = {}

        # 1. 为每个工具创建异步任务
        for tc in valid_tool_calls:
            call_id = tc.get("id", f"call_{id(tc)}")
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            arguments = func.get("arguments", "{}")

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    results_map[call_id] = {
                        "tool_call_id": call_id,
                        "output": "",
                        "error": f"JSON解析失败: '{arguments}'. {e}",
                    }
                    continue

            task = asyncio.create_task(
                self._execute_single(call_id, tool_name, arguments),
                name=f"tool-{tool_name}-{call_id[:8]}",
            )
            tasks[call_id] = task

        # 2. 等待所有任务完成（支持中途 abort）
        # 若提供 abort_event，则与 abort_wait 任务竞争；abort 触发时取消未完成 task。
        synthetic_messages: List[ToolMessage] = []
        try:
            if abort_event is not None:
                abort_waiter = asyncio.create_task(
                    abort_event.wait(), name="abort-waiter"
                )
                wait_targets = list(tasks.values()) + [abort_waiter]
                try:
                    await asyncio.wait(
                        wait_targets,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # 若 abort 触发但仍有未完成 task，继续等剩余、或全部取消
                    while abort_event.is_set() and any(not t.done() for t in tasks.values()):
                        for t in tasks.values():
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(*tasks.values(), return_exceptions=True)
                        break
                    # 未 abort 时，继续等剩余任务
                    if not abort_event.is_set():
                        await asyncio.gather(*tasks.values(), return_exceptions=True)
                finally:
                    if not abort_waiter.done():
                        abort_waiter.cancel()
                        try:
                            await abort_waiter
                        except (asyncio.CancelledError, Exception):
                            pass
            else:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
        except asyncio.CancelledError:
            # 被外部取消 → 取消所有子任务并回收
            for t in tasks.values():
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise

        # 3. 收集结果
        for call_id, task in tasks.items():
            if task.cancelled() or (abort_event and abort_event.is_set() and not task.done()):
                if not task.done():
                    task.cancel()
                synthetic_messages.append(
                    ToolMessage(
                        tool_call_id=call_id,
                        content="[System Interrupt] 会话被中断，工具未执行完成。",
                        is_synthetic=True,
                    )
                )
                continue

            if task.done():
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    results_map[call_id] = {
                        "tool_call_id": call_id,
                        "output": "",
                        "error": f"[Execution Error] {exc}",
                    }
                elif exc is None:
                    results_map[call_id] = task.result()
                else:
                    synthetic_messages.append(
                        ToolMessage(
                            tool_call_id=call_id,
                            content="[System Interrupt] 工具被取消。",
                            is_synthetic=True,
                        )
                    )

        # 4. 按原始顺序返回（便于 strategy 保持可预测性）
        results = []
        for tc in valid_tool_calls:
            call_id = tc.get("id", f"call_{id(tc)}")
            if call_id in results_map:
                results.append(results_map[call_id])

        return results, synthetic_messages

    async def _execute_single(
        self,
        call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        执行单个工具调用，受 asyncio.shield 保护的安全关键路径。

        步骤：
        1. security.intercept（同步 → to_thread）
        2. registry.get + tool.execute（同步 → to_thread）
        3. 异常兜底
        """
        try:
            # ---- asyncio.shield：安全审计不容中断 ----
            allowed, error_msg = await asyncio.shield(
                asyncio.to_thread(
                    self._engine.security.intercept,
                    tool_name,
                    arguments,
                    self._session_id,
                )
            )
            if not allowed:
                return {
                    "tool_call_id": call_id,
                    "output": f"[Security Blocked] 安全拦截: {error_msg}。",
                }

            # ---- 工具查找与执行 ----
            tool = self._engine.registry.get(tool_name)
            if tool is None:
                return {
                    "tool_call_id": call_id,
                    "output": f"[Tool Not Found] 工具 '{tool_name}' 不存在。",
                }

            result = await asyncio.to_thread(tool.execute, **arguments)
            output = str(result)

            # 单条输出截断（与 strategy._stage_3 一致）
            MAX_SINGLE_OUTPUT_LIMIT = 40000
            if len(output) > MAX_SINGLE_OUTPUT_LIMIT:
                output = (
                    output[:MAX_SINGLE_OUTPUT_LIMIT]
                    + f"\n\n...[内容过长: 剩余 {len(output) - MAX_SINGLE_OUTPUT_LIMIT} 字符已被系统强制截断]..."
                )

            return {"tool_call_id": call_id, "output": output}

        except asyncio.CancelledError:
            # shield 内的 CancelledError 需重新抛出
            raise
        except Exception as e:
            logger.exception(f"Tool '{tool_name}' execution failed")
            return {
                "tool_call_id": call_id,
                "output": "",
                "error": f"[Execution Error] {tool_name}: {e}",
            }


__all__ = ["StreamingExecutor"]