"""
Strategy module for agent execution flow.
Upgraded into 4 distinct industrial stages inspired by Anthropic Claude Code.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, List, Dict
import json
from harness_lite.config.loader import get_llm_config

config = get_llm_config()

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
    """

    def __init__(self, max_steps: int = 15, max_tokens_threshold: int = 64000):
        """
        初始化策略。
        Args:
            max_steps: 最大允许的思考循环步数限制
            max_tokens_threshold: 触发上下文记忆收缩的 Token 高水位线
        """

        from harness_lite.context.manager import DynamicContextManager
        self.max_steps = max_steps
        self.context_manager = DynamicContextManager(max_allowed_tokens=max_tokens_threshold)
        self.MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3

    async def execute(self, task: str, engine: Any, session_id: str,
                      stream_callback: Optional[Callable[[str], None]] = None,
                      status_callback: Optional[Callable[[str], None]] = None) -> str:
        messages = engine.build_hot_swapped_context(task, session_id)

        step = 0
        full_response = ""
        consecutive_errors = 0
        max_output_tokens_recovery_count = 0
        tracking = None

        while step < self.max_steps:
            step += 1

            messages = await self._stage_1_context_optimization(messages, engine, session_id, status_callback)

            response = await engine.call_llm_async(
                messages, stream=True, stream_callback=stream_callback, status_callback=status_callback
            )

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            assistant_content = assistant_message.get("content", "")
            tool_calls = assistant_message.get("tool_calls", [])
            finish_reason = response.get("choices", [{}])[0].get("finish_reason", "stop")

            if finish_reason == "length":
                if max_output_tokens_recovery_count < self.MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
                    max_output_tokens_recovery_count += 1
                    if status_callback:
                        status_callback(f"[⚠️ 状态自愈] 检测到输出触顶截断，正在原位注入跨 Turn 无缝续写指令 (第 {max_output_tokens_recovery_count} 次)...")

                    messages.append({
                        "role": "assistant",
                        "content": assistant_content + "\n...[由于输出字数限制，此处被系统安全截断]..."
                    })
                    messages.append({
                        "role": "user",
                        "content": "输出字数限制已达到。请从您中断思考的地方直接继续进行。无需道歉，也无需回顾您之前正在做的事情。继续进行。",
                        "is_meta": True
                    })
                    full_response += assistant_content
                    continue
            if tool_calls:
                messages, has_error = await self._stage_3_tool_orchestration(
                    messages, tool_calls, engine, session_id, assistant_content, assistant_message, status_callback
                )

                if has_error:
                    consecutive_errors += 1
                    if status_callback:
                        status_callback(f"[⚠️ 纠错中] 链路工具流执行异常 (连续 {consecutive_errors} 次)，引导大模型自我修正...")
                    if consecutive_errors >= 3:
                        break_msg = "\n[系统硬熔断] 工具流连续调用失败过多或触犯安全红线，已强制终止本次推理循环。"
                        if stream_callback:
                            stream_callback(break_msg)
                        messages.append({
                            "role": "assistant",
                            "content": break_msg
                        })
                        full_response += break_msg
                        break
                else:
                    consecutive_errors = 0
                continue
            full_response += assistant_content
            await self._stage_4_state_persistence(messages, assistant_content, assistant_message, engine, session_id)
            return full_response

        if step >= self.max_steps:
            final_response = f"\n[系统提示] 已达到最大思考步数限制 ({self.max_steps}步)，强制停止执行以防死循环。"
            if stream_callback:
                stream_callback(final_response)
            messages.append({
                "role": "assistant",
                "content": final_response
            })
            engine.memory.save_context(session_id, messages)
        return full_response

    async def _stage_1_context_optimization(self, messages: List[Dict], engine: Any, session_id: str, status_callback: Callable) -> List[Dict]:
        """
        【STAGE 1】上下文优化层：负责处理 Token 高水位检测与动态收缩剪枝
        """
        from harness_lite.tools.execution_ops import process_manager
        active_shell = process_manager.get_shell(session_id)
        current_terminal_cwd = active_shell.last_known_cwd if active_shell else "/"
        return await self.context_manager.compress_if_overflow(
            messages=messages,
            engine=engine,
            current_cwd=current_terminal_cwd,
            status_callback=status_callback
        )

    async def _stage_3_tool_orchestration(self, messages: List[Dict], tool_calls: List[Dict], engine: Any,
                                          session_id: str, assistant_content: str, assistant_message: Dict,
                                          status_callback: Callable) -> tuple[List[Dict], bool]:
        """
        【STAGE 3】工具异步编排与自愈层：负责工具并发调度及 Fail-Fast 级联中断
        """

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
            elif str(output_val).startswith(("[Security", "[Tool Not", "[Execution Error]", "静态防御", "语义审计", "[Cancelled]")):
                content = output_val
                has_error_in_this_step = True
            else:
                content = str(output_val)

            MAX_SINGLE_OUTPUT_LIMIT = 40000
            if len(content) > MAX_SINGLE_OUTPUT_LIMIT:
                content = content[:MAX_SINGLE_OUTPUT_LIMIT] + f"\n\n...[内容过长: 剩余 {len(content) - MAX_SINGLE_OUTPUT_LIMIT} 字符已被系统强制截断]..."

            messages.append({
                "role": "tool",
                "tool_call_id": result.get("tool_call_id"),
                "content": content
            })
        if not has_error_in_this_step and valid_tool_calls and status_callback:
            status_callback(f"[✅ 已完成] 阶段工具数据回传成功，交由主模型总结...")
        return messages, has_error_in_this_step

    async def _stage_4_state_persistence(self, messages: List[Dict], assistant_content: str, assistant_message: Dict, engine: Any, session_id: str):
        """【STAGE 4】状态持久化与环境反哺层：负责激进保存冷历史，确保进程意外中止时会话完全可 Resume 恢复"""
        assistant_payload = {
            "role": "assistant",
            "content": assistant_content
        }
        if config.get("thinking_mode") and assistant_message.get("reasoning_content"):
            assistant_payload["reasoning_content"] = assistant_message["reasoning_content"]
        messages.append(assistant_payload)

        engine.memory.save_context(session_id, messages)
