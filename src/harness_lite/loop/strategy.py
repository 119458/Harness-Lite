"""
Strategy module for agent execution flow.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, List, Dict

class BaseStrategy(ABC):
    """编排策略基类"""

    @abstractmethod
    async def execute(self, task: str, engine: Any, session_id: str, stream_callback: Optional[Callable[[str], None]] = None) -> str:
        """
        执行策略的抽象方法
        """
        pass

class ReActStrategy(BaseStrategy):
    """健壮的 ReAct 循环策略，自带上下文保护与熔断机制"""

    def __init__(self, max_steps: int = 15, max_context_length: int = 12):
        self.max_steps = max_steps
        self.max_context_length = max_context_length

    def _prune_context(self, messages: List[Dict[str, Any]], status_callback: Optional[Callable[[str], None]]) -> List[Dict[str, Any]]:
        """
        安全地修剪上下文：滑动窗口机制
        必须保证 OpenAI 格式的严格性：'tool' 角色消息不能失去对应的 'assistant' (tool_calls) 消息。
        """
        if len(messages) <= self.max_context_length:
            return messages

        core_context = messages[:2]
        recent_context = []

        idx = len(messages) - 1
        while idx >= 2 and len(recent_context) < (self.max_context_length - 2):
            recent_context.insert(0, messages[idx])
            idx -= 1
        while recent_context and recent_context[0].get("role") == "tool":
            recent_context.pop(0)
        if status_callback and len(messages) != len(core_context + recent_context):
            status_callback("[🧹 内存优化] 历史会话过长，已自动折叠早期的工具执行日志，释放上下文空间。")

        return core_context + recent_context

    async def execute(self, task: str, engine: Any, session_id: str, stream_callback: Optional[Callable[[str], None]] = None, status_callback: Optional[Callable[[str], None]] = None) -> str:
        # 1. 组装初始化上下文
        messages = engine.memory.load_context(session_id)
        if not messages:
            messages = engine.build_initial_messages(task)
        else:
            messages.append({"role": "user", "content": task})

        step = 0
        full_response = ""
        consecutive_errors = 0 # 连续错误计数器

        while step < self.max_steps:
            step += 1

            messages = self._prune_context(messages, status_callback)
            # 步骤A：推理（调用LLM）
            is_streaming = stream_callback is not None
            response = await engine.call_llm_async(messages, is_streaming, stream_callback=stream_callback, status_callback=status_callback)

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
                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                })

                # 异步执行工具
                tool_results = await engine.process_tool_calls_async(tool_calls, session_id)

                has_error_in_this_step = False

                # 收集工具结果并写回上下文
                for result in tool_results:
                    error_val = result.get("error")
                    output_val = result.get("output", "")
                    if error_val:
                        content = error_val
                        has_error_in_this_step = True
                    elif str(output_val).startswith("[Security") or str(output_val).startswith("[Tool Not") or str(output_val).startswith("[Execution Error]"):
                        content = output_val
                        has_error_in_this_step = True
                    else:
                        content = str(output_val)
                    MAX_TOOL_OUTPUT_LEN = 4000
                    if len(content) > MAX_TOOL_OUTPUT_LEN:
                        content = content[:MAX_TOOL_OUTPUT_LEN] + f"\n\n...[内容过长: 剩余 {len(content) - MAX_TOOL_OUTPUT_LEN} 字符已被系统强制截断以保护上下文]..."

                    messages.append({
                        "role": "tool",
                        "tool_call_id": result.get("tool_call_id"),
                        "content": content
                    })
                if has_error_in_this_step:
                    consecutive_errors += 1
                    if status_callback:
                        status_callback(
                            f"[⚠️ 纠错中] 工具执行异常 (连续 {consecutive_errors} 次)，模型正在自我修正..."
                        )
                    if consecutive_errors >= 3:
                        break_msg = "\n[系统熔断] 工具连续调用失败过多，已强制终止本次推理。"
                        if stream_callback:
                            stream_callback(break_msg)
                        messages.append({"role": "assistant", "content": break_msg})
                        full_response += break_msg
                        break
                else:
                    consecutive_errors = 0
                    if valid_tool_calls and status_callback:
                        status_callback(f"[✅ 已完成] 工具数据获取成功，交由大模型总结...")
                # 继续下一轮循环，让LLM根据工具结果作答
                continue

            # 步骤 D：判断终态 (如果没有 Tool Calls，说明是纯文本回复)
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})
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

