"""
Strategy module for agent execution flow.

Upgraded into 4 distinct industrial stages inspired by Anthropic Claude Code.

阶段 C 重构要点（master.md 4.3）：
- 新增 `execute_stream()` AsyncGenerator：逐步 yield LoopMessage
- 保留 `execute() -> str` 兼容方法：内部消费 execute_stream，fan-out 到 callback，返回最终文本
- 引入 RecoveryBudget 集中管理所有恢复计数器
- 显式异常分类（recovery.py），禁止裸 except Exception
- length 恢复 + reactive compact + consecutive_errors 熔断 通通走 budget
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Callable, Optional, List, Dict
import asyncio
import json
import logging

from harness_lite.config.loader import get_main_config
from harness_lite.loop.messages import (
    AssistantMessage,
    AttachmentMessage,
    LoopMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from harness_lite.loop.terminal import Terminal
from harness_lite.loop.recovery import (
    RecoveryBudget,
    RecoveryAction,
    classify_finish_reason,
    classify_llm_exception,
    build_length_recovery_messages,
)

logger = logging.getLogger("harness_lite.strategy")

# TODO(三模型差异化): 后续可切换为 get_small_config() / get_medium_config()
config = get_main_config()


class BaseStrategy(ABC):
    """编排策略基类"""

    @abstractmethod
    async def execute(self, task: str, engine: Any, session_id: str,
                      stream_callback: Optional[Callable[[str], None]] = None,
                      status_callback: Optional[Callable[[str], None]] = None) -> str:
        pass


class ReActStrategy(BaseStrategy):
    """
    高级 4 阶段 ReAct 循环策略。
    具备 Token 自适应收缩、Max Tokens 截断无感续写缝合与工具 Fail-Fast 级联熔断网。

    阶段 C 新协议：
    - execute_stream() : AsyncGenerator[LoopMessage, None]  ← 新增主入口
    - execute()        : str  ← 兼容旧调用栈（B2 QueryEngine 仍走这条）
    """

    def __init__(self, max_steps: int = 15, max_tokens_threshold: int = 64000):
        from harness_lite.context.manager import DynamicContextManager
        self.max_steps = max_steps
        self.context_manager = DynamicContextManager(max_allowed_tokens=max_tokens_threshold)

    # ================================================================
    # 1. 兼容旧调用栈的 execute() —— 内部消费 execute_stream
    # ================================================================

    async def execute(self, task: str, engine: Any, session_id: str,
                      stream_callback: Optional[Callable[[str], None]] = None,
                      status_callback: Optional[Callable[[str], None]] = None) -> str:
        """
        【B2 兼容入口】消费 execute_stream 并 fan-out 到 callback，最终返回字符串。

        与 B1/B2 之前的行为完全等价：
        - stream_callback 接收纯文本片段（来自 AssistantMessage.content）
        - status_callback 接收状态行（仍由底层 engine._stream_llm_events 推送）
        """
        full_text = ""

        async for msg in self.execute_stream(
            task=task,
            engine=engine,
            session_id=session_id,
            stream_callback=stream_callback,
            status_callback=status_callback,
        ):
            if isinstance(msg, AssistantMessage) and not msg.is_meta:
                # 最终一次的 AssistantMessage.content 是完整回复
                # （中间轮工具调用的 assistant 消息不会作为最终结果 yield）
                full_text = msg.content
            elif isinstance(msg, SystemMessage) and msg.subtype == "error_during_execution":
                # 异常路径：把错误信息作为最终文本返回
                full_text = msg.content

        return full_text

    # ================================================================
    # 2. 新核心：execute_stream() AsyncGenerator
    # ================================================================

    async def execute_stream(
        self,
        task: str,
        engine: Any,
        session_id: str,
        stream_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> AsyncGenerator[LoopMessage, None]:
        """
        【阶段 C 主入口】真正的 ReAct While-True 循环，yield LoopMessage。

        每轮迭代步骤：
        1. 上下文压缩（_stage_1_context_optimization）
        2. 流式 LLM 调用（engine.call_llm_async，B1 已 generator 化但仍聚合返回）
        3. 错误恢复判定（recovery.classify_finish_reason / classify_llm_exception）
        4. 工具执行（_stage_3_tool_orchestration）
        5. 状态持久化（_stage_4_state_persistence，仅在 turn 终止时调用）

        终止路径（必走 finally）：
        - 正常 completed → AssistantMessage 作为最终结果
        - length 恢复耗尽 → SystemMessage(error_during_execution) + Terminal.MODEL_ERROR
        - reactive compact 后仍超长 → SystemMessage + Terminal.PROMPT_TOO_LONG
        - 工具连续异常熔断 → SystemMessage + Terminal.HOOK_STOPPED
        - max_steps 触顶 → SystemMessage + Terminal.MAX_TURNS
        - asyncio.CancelledError / KeyboardInterrupt → 必须重抛
        """
        messages = engine.build_hot_swapped_context(task, session_id)
        budget = RecoveryBudget()
        step = 0
        full_response = ""
        last_assistant_message: Optional[Dict[str, Any]] = None
        terminal: Optional[Terminal] = None
        terminated_normally = False

        try:
            while step < self.max_steps:
                step += 1

                # ---- STAGE 1: 上下文优化 ----
                messages = await self._stage_1_context_optimization(
                    messages, engine, session_id, status_callback
                )

                # ---- STAGE 2: 流式 LLM 调用 + 异常分类 ----
                try:
                    response = await engine.call_llm_async(
                        messages,
                        stream=True,
                        stream_callback=stream_callback,
                        status_callback=status_callback,
                    )
                except asyncio.CancelledError:
                    raise
                except KeyboardInterrupt:
                    raise
                except BaseException as exc:
                    decision = classify_llm_exception(exc, budget)
                    if decision.action == RecoveryAction.RERAISE:
                        raise
                    if decision.action == RecoveryAction.REACTIVE_COMPACT_RETRY:
                        budget.mark_reactive_compact_attempted()
                        if status_callback:
                            status_callback("[💾 上下文压缩] 触发被动压缩重试...")
                        messages = await self._force_compact(messages, engine, session_id, status_callback)
                        continue
                    # 其余皆为 TERMINATE
                    terminal = decision.terminal or Terminal.MODEL_ERROR
                    err_text = f"\n[系统错误] {decision.reason}"
                    if stream_callback:
                        stream_callback(err_text)
                    full_response += err_text
                    break

                assistant_message = response.get("choices", [{}])[0].get("message", {})
                assistant_content = assistant_message.get("content", "") or ""
                tool_calls = assistant_message.get("tool_calls") or []
                finish_reason = response.get("choices", [{}])[0].get("finish_reason", "stop")
                last_assistant_message = assistant_message

                # ---- STAGE 2.5: finish_reason 恢复判定 ----
                if finish_reason == "length":
                    decision = classify_finish_reason(finish_reason, budget)
                    if decision.action == RecoveryAction.INJECT_LENGTH_NUDGE:
                        count = budget.consume_length_recovery()
                        if status_callback:
                            status_callback(
                                f"[⚠️ 状态自愈] 检测到输出触顶截断，"
                                f"正在原位注入跨 Turn 无缝续写指令 (第 {count} 次)..."
                            )
                        messages.extend(build_length_recovery_messages(assistant_content))
                        full_response += assistant_content
                        continue
                    # 否则进入 TERMINATE 路径
                    terminal = decision.terminal or Terminal.MODEL_ERROR
                    err_text = f"\n[系统提示] {decision.reason}"
                    if stream_callback:
                        stream_callback(err_text)
                    full_response += assistant_content + err_text
                    break

                # ---- STAGE 3: 工具调用分支 ----
                if tool_calls:
                    messages, has_error = await self._stage_3_tool_orchestration(
                        messages, tool_calls, engine, session_id,
                        assistant_content, assistant_message, status_callback,
                    )

                    if has_error:
                        n = budget.record_tool_error()
                        if status_callback:
                            status_callback(
                                f"[⚠️ 纠错中] 链路工具流执行异常 (连续 {n} 次)，引导大模型自我修正..."
                            )
                        if budget.is_tool_error_fused():
                            break_msg = "\n[系统硬熔断] 工具流连续调用失败过多或触犯安全红线，已强制终止本次推理循环。"
                            if stream_callback:
                                stream_callback(break_msg)
                            messages.append({"role": "assistant", "content": break_msg})
                            full_response += break_msg
                            terminal = Terminal.HOOK_STOPPED
                            break
                    else:
                        budget.reset_tool_errors()
                    continue

                # ---- 正常完成路径 ----
                full_response += assistant_content
                terminal = Terminal.COMPLETED
                terminated_normally = True
                await self._stage_4_state_persistence(
                    messages, assistant_content, assistant_message, engine, session_id
                )
                yield AssistantMessage(
                    content=full_response,
                    reasoning_content=assistant_message.get("reasoning_content"),
                )
                return

            # ---- max_steps 兜底 ----
            if step >= self.max_steps and terminal is None:
                final_response = (
                    f"\n[系统提示] 已达到最大思考步数限制 ({self.max_steps}步)，"
                    f"强制停止执行以防死循环。"
                )
                if stream_callback:
                    stream_callback(final_response)
                messages.append({"role": "assistant", "content": final_response})
                full_response += final_response
                terminal = Terminal.MAX_TURNS

            # ---- 非正常路径的 yield ----
            if not terminated_normally:
                yield SystemMessage(
                    content=full_response,
                    subtype="error_during_execution",
                )

        except asyncio.CancelledError:
            # 必须重抛；finally 负责落盘兜底
            terminal = Terminal.ABORTED_STREAMING
            raise
        except KeyboardInterrupt:
            terminal = Terminal.ABORTED
            raise
        finally:
            # 兜底持久化：异常路径下也要保存 messages，防止上下文丢失
            if not terminated_normally:
                try:
                    engine.memory.save_context(session_id, messages)
                except Exception as e:
                    logger.warning(f"[strategy] finally save_context failed: {e}")

    # ================================================================
    # 3. 各阶段实现（继承自 B2 之前的逻辑，仅做最小修改）
    # ================================================================

    async def _stage_1_context_optimization(
        self,
        messages: List[Dict],
        engine: Any,
        session_id: str,
        status_callback: Optional[Callable],
    ) -> List[Dict]:
        """【STAGE 1】上下文优化层：Token 高水位检测与动态收缩剪枝。"""
        from harness_lite.tools.bash_terminal import process_manager
        active_shell = process_manager.get_shell(session_id)
        current_terminal_cwd = active_shell.last_known_cwd if active_shell else "/"
        return await self.context_manager.compress_if_overflow(
            messages=messages,
            engine=engine,
            current_cwd=current_terminal_cwd,
            status_callback=status_callback,
        )

    async def _force_compact(
        self,
        messages: List[Dict],
        engine: Any,
        session_id: str,
        status_callback: Optional[Callable],
    ) -> List[Dict]:
        """
        【reactive compact 入口】上下文超长异常恢复时使用。

        与 stage_1 的区别：不检查阈值，强制压缩。
        一期实现：直接降低阈值再调一次 compress_if_overflow（保证一定触发）。
        """
        from harness_lite.tools.bash_terminal import process_manager
        active_shell = process_manager.get_shell(session_id)
        current_terminal_cwd = active_shell.last_known_cwd if active_shell else "/"

        # 临时降阈值强制触发压缩
        original_threshold = self.context_manager.max_allowed_tokens
        try:
            self.context_manager.max_allowed_tokens = 100  # 极低阈值，强制压缩
            compressed = await self.context_manager.compress_if_overflow(
                messages=messages,
                engine=engine,
                current_cwd=current_terminal_cwd,
                status_callback=status_callback,
            )
            return compressed
        finally:
            self.context_manager.max_allowed_tokens = original_threshold

    async def _stage_3_tool_orchestration(
        self,
        messages: List[Dict],
        tool_calls: List[Dict],
        engine: Any,
        session_id: str,
        assistant_content: str,
        assistant_message: Dict,
        status_callback: Optional[Callable],
    ) -> tuple[List[Dict], bool]:
        """【STAGE 3】工具异步编排与自愈层：负责工具并发调度及 Fail-Fast 级联中断。"""

        valid_tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name")]
        if valid_tool_calls and status_callback:
            tool_names = [tc["function"]["name"] for tc in valid_tool_calls]
            status_callback(f"[⚙️ 线程激活] 正在并发调度工具: {', '.join(tool_names)} ...")

        assistant_payload = {
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": tool_calls,
        }
        if config.get("thinking_mode") and assistant_message.get("reasoning_content"):
            assistant_payload["reasoning_content"] = assistant_message["reasoning_content"]
        messages.append(assistant_payload)

        tool_results = await engine.process_tool_calls_async(tool_calls, session_id)
        has_error_in_this_step = False

        for result in tool_results:
            error_val = result.get("error")
            output_val = result.get("output", "")

            if error_val:
                content = error_val
                has_error_in_this_step = True
            elif str(output_val).startswith(
                ("[Security", "[Tool Not", "[Execution Error]", "静态防御", "语义审计", "[Cancelled]")
            ):
                content = output_val
                has_error_in_this_step = True
            else:
                content = str(output_val)

            MAX_SINGLE_OUTPUT_LIMIT = 40000
            if len(content) > MAX_SINGLE_OUTPUT_LIMIT:
                content = (
                    content[:MAX_SINGLE_OUTPUT_LIMIT]
                    + f"\n\n...[内容过长: 剩余 {len(content) - MAX_SINGLE_OUTPUT_LIMIT} 字符已被系统强制截断]..."
                )

            messages.append({
                "role": "tool",
                "tool_call_id": result.get("tool_call_id"),
                "content": content,
            })

        if not has_error_in_this_step and valid_tool_calls and status_callback:
            status_callback("[✅ 已完成] 阶段工具数据回传成功，交由主模型总结...")
        return messages, has_error_in_this_step

    async def _stage_4_state_persistence(
        self,
        messages: List[Dict],
        assistant_content: str,
        assistant_message: Dict,
        engine: Any,
        session_id: str,
    ) -> None:
        """【STAGE 4】状态持久化与环境反哺层：仅在 turn 正常终止时调用一次。"""
        assistant_payload = {
            "role": "assistant",
            "content": assistant_content,
        }
        if config.get("thinking_mode") and assistant_message.get("reasoning_content"):
            assistant_payload["reasoning_content"] = assistant_message["reasoning_content"]
        messages.append(assistant_payload)

        engine.memory.save_context(session_id, messages)
