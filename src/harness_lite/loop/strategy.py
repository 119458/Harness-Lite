"""
Strategy module for agent execution flow.
Upgraded with dynamic token-based context compression and multi-tenant safety.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, List, Dict
import json

# 【核心添加点】引入全新的一阶段动态上下文管理器
from harness_lite.context.manager import DynamicContextManager
from harness_lite.config.loader import get_llm_config

config = get_llm_config()


class BaseStrategy(ABC):
    """编排策略基类"""

    @abstractmethod
    async def execute(self, task: str, engine: Any, session_id: str,
                      stream_callback: Optional[Callable[[str], None]] = None) -> str:
        """
        执行策略的抽象方法
        """
        pass


class ReActStrategy(BaseStrategy):
    """高级 ReAct 循环策略，自带 Token 级自适应‘记忆收缩’上下文管理与多层安全反哺机制"""

    def __init__(self, max_steps: int = 15, max_tokens_threshold: int = 64000):
        """
        初始化策略。

        Args:
            max_steps: 最大允许的思考循环步数限制
            max_tokens_threshold: 触发上下文记忆收缩的 Token 高水位线
        """
        self.max_steps = max_steps
        # 初始化上下文管理器插槽
        self.context_manager = DynamicContextManager(max_allowed_tokens=max_tokens_threshold)

    async def execute(self, task: str, engine: Any, session_id: str,
                      stream_callback: Optional[Callable[[str], None]] = None,
                      status_callback: Optional[Callable[[str], None]] = None) -> str:
        # 1. 组装并初始化多租户上下文，将当前的 session_id 稳妥向下透传
        messages = engine.memory.load_context(session_id)
        if not messages:
            messages = engine.build_hot_swapped_context(task, session_id=session_id)
        else:
            messages.append({"role": "user", "content": task})

        step = 0
        full_response = ""
        consecutive_errors = 0  # 连续错误计数器

        while step < self.max_steps:
            step += 1

            # ========================================================
            # 【🔥 阶段一落地核心】：废除原数行截断，调用 Token 级自适应记忆收缩引擎
            # ========================================================
            from harness_lite.tools.execution_ops import process_manager
            active_shell = process_manager.get_shell(session_id)
            current_terminal_cwd = active_shell.last_known_cwd
            messages = await self.context_manager.compress_if_overflow(
                messages=messages,
                engine=engine,
                current_cwd=current_terminal_cwd,
                status_callback=status_callback
            )

            # 步骤A：推理（调用LLM）
            is_streaming = stream_callback is not None
            response = await engine.call_llm_async(messages, is_streaming, stream_callback=stream_callback,
                                                   status_callback=status_callback)

            # 步骤B：解析意图
            assistant_message = response.get("choices", [{}])[0].get("message", {})
            assistant_content = assistant_message.get("content", "")
            tool_calls = assistant_message.get("tool_calls", [])

            # 步骤C：执行动作（如果存在Tool Calls）
            if tool_calls:
                valid_tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name")]
                if valid_tool_calls and status_callback:
                    tool_names = [tc["function"]["name"] for tc in valid_tool_calls]
                    names_str = ", ".join(tool_names)
                    status_callback(f"[⚙️ 执行中] 正在并发调度工具: {names_str} ...")

                # 记录模型的思考与工具调用请求
                assistant_payload = {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                }
                if config.get("thinking_mode") and assistant_message.get("reasoning_content"):
                    assistant_payload["reasoning_content"] = assistant_message["reasoning_content"]
                messages.append(assistant_payload)

                # 异步执行工具并透传会话租户状态
                tool_results = await engine.process_tool_calls_async(tool_calls, session_id)

                has_error_in_this_step = False

                # 收集工具结果并写回上下文
                for result in tool_results:
                    error_val = result.get("error")
                    output_val = result.get("output", "")
                    if error_val:
                        content = error_val
                        has_error_in_this_step = True
                    # 安全反哺：如果触发了静态防御拦截、语义大模型阻断，或人工 HITL 拒绝，将其视为行为熔断标志
                    elif str(output_val).startswith(
                            ("[Security", "[Tool Not", "[Execution Error]", "静态防御", "语义审计")):
                        content = output_val
                        has_error_in_this_step = True
                    else:
                        content = str(output_val)

                    # 动态上下文管理器会对单次大输出进行安全兜底截断
                    MAX_SINGLE_OUTPUT_LIMIT = 40000
                    if len(content) > MAX_SINGLE_OUTPUT_LIMIT:
                        content = content[
                                  :MAX_SINGLE_OUTPUT_LIMIT] + f"\n\n...[内容过长: 剩余 {len(content) - MAX_SINGLE_OUTPUT_LIMIT} 字符已被系统强制截断以保护上下文]..."

                    messages.append({
                        "role": "tool",
                        "tool_call_id": result.get("tool_call_id"),
                        "content": content
                    })

                if has_error_in_this_step:
                    consecutive_errors += 1
                    if status_callback:
                        status_callback(
                            f"[⚠️ 纠错中] 链路执行异常反馈 (连续 {consecutive_errors} 次)，引导大模型自我修正中..."
                        )
                    if consecutive_errors >= 3:
                        break_msg = "\n[系统熔断] 工具流连续调用失败过多或触犯安全红线，已强制终止本次推理循环。"
                        if stream_callback:
                            stream_callback(break_msg)
                        messages.append({"role": "assistant", "content": break_msg})
                        full_response += break_msg
                        break
                else:
                    consecutive_errors = 0
                    if valid_tool_calls and status_callback:
                        status_callback(f"[✅ 已完成] 阶段工具数据获取成功，交由主模型总结...")

                # 继续下一轮循环，让LLM根据脱水或原始的工具结果作答
                continue

            # 步骤 D：判断终态 (如果没有 Tool Calls，说明是纯文本回复)
            if assistant_content:
                assistant_payload = {"role": "assistant", "content": assistant_content}
                if config.get("thinking_mode") and assistant_message.get("reasoning_content"):
                    assistant_payload["reasoning_content"] = assistant_message["reasoning_content"]
                messages.append(assistant_payload)
                full_response += assistant_content
            elif not tool_calls:
                error_msg = "\n[系统兜底] 模型返回了空内容，请检查大模型服务是否正常。"
                messages.append({"role": "assistant", "content": error_msg})
                full_response += error_msg

            engine.memory.save_context(session_id, messages)
            return full_response

        # 步骤 E：达到最大步数
        if step >= self.max_steps:
            final_response = f"\n[系统提示] 已达到最大思考步数限制 ({self.max_steps}步)，强制停止执行以防死循环。"
            if stream_callback:
                stream_callback(final_response)
            messages.append({"role": "assistant", "content": final_response})
        engine.memory.save_context(session_id, messages)
        return full_response