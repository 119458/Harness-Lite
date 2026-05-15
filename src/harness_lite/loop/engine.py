"""Loop engine module.

Core LLM loop engine that autonomously decides, calls tools, and verifies results.
"""

from typing import Dict, Any, List, Optional, Callable
import requests

from harness_lite.memory.manager import MemoryManager
from harness_lite.registry import tool_registry
from harness_lite.security.manager import security_manager
from harness_lite.config.loader import get_llm_config


SYSTEM_PROMPT = """你是一个智能助手，可以使用工具来完成任务。

可用工具：
{tools_schema}

当你需要完成一个任务时：
1. 如果可以直接回答，直接回复
2. 如果需要调用工具，使用 tool_calls 格式
3. 完成工具调用后，根据结果回复用户

记住：
- 所有工具调用必须提供完整的参数
- 只有在真正需要时才调用工具
"""


class LoopEngine:
    """Core LLM loop engine with tool calling capability."""

    def __init__(self, session_id: str = "default"):
        """
        Initialize the loop engine.

        Args:
            session_id: Session ID for memory management
        """
        self.session_id = session_id
        self._memory = MemoryManager()
        self._security = security_manager
        self._registry = tool_registry

    def run(self, task: str, session_id: str, stream_callback: Callable[[str], None] = None) -> str:
        """
        Execute a task with tool calling loop.

        Args:
            task: User input task
            session_id: Session ID for memory context
            stream_callback: Optional callback for streaming responses

        Returns:
            LLM generated response
        """
        self.session_id = session_id
        self._stream_callback = stream_callback

        # Load historical context from memory
        messages = self._memory.load_context(session_id)

        # If no history, build messages with system prompt (including tools schema)
        if not messages:
            messages = self._build_messages(task)
        else:
            # Add user message to existing history
            messages.append({"role": "user", "content": task})

        # Tool call loop
        max_iterations = 20
        iteration = 0
        full_response = ""

        while iteration < max_iterations:
            iteration += 1

            # Call LLM with streaming if callback provided
            is_streaming = stream_callback is not None
            response = self._call_llm(messages, stream=is_streaming)

            # Extract assistant message
            assistant_message = response.get("choices", [{}])[0].get("message", {})
            assistant_content = assistant_message.get("content", "") or ""
            tool_calls = assistant_message.get("tool_calls") or []

            # If there are tool calls, process them
            if tool_calls:
                # Add assistant message to history
                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls
                })

                # Process tool calls
                tool_results = self._process_tool_calls(tool_calls)

                # Add tool results to messages
                for result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": result.get("tool_call_id"),
                        "content": result.get("output", str(result.get("error", "Unknown error")))
                    })
                # Continue to next iteration to get final response
                continue

            # No tool calls - this is the final response
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})
                full_response += assistant_content

            # Save context to memory before returning
            self._memory.save_context(session_id, messages)
            return full_response

        # Max iterations reached
        final_response = "已达到最大迭代次数，请稍后重试。"
        messages.append({"role": "assistant", "content": final_response})
        self._memory.save_context(session_id, messages)
        return final_response

    def _build_messages(self, task: str) -> List[Dict[str, str]]:
        """
        Build message list for LLM.

        Args:
            task: User input

        Returns:
            Message list
        """
        tools_schema = self._get_all_tools_schema()
        system_content = SYSTEM_PROMPT.format(tools_schema=tools_schema)

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": task}
        ]

    def _call_llm(self, messages: List[Dict[str, Any]], stream: bool = False) -> Dict[str, Any]:
        """
        Call LLM API.

        Args:
            messages: Message list
            stream: Whether to use streaming response

        Returns:
            LLM response
        """
        config = get_llm_config()
        tools = self._get_all_tools_schema()

        if stream:
            return self._call_llm_stream(messages, tools)
        else:
            response = requests.post(
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": config["model_name"],
                    "messages": messages,
                    "tools": tools if tools else None
                },
                timeout=60
            )
            return response.json()

    def _call_llm_stream(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Call LLM API with streaming response.

        Args:
            messages: Message list
            tools: Tool schemas

        Returns:
            LLM response (reconstructed from stream)
        """
        config = get_llm_config()
        full_content = ""
        collected_tool_calls = []

        payload = {
            "model": config["model_name"],
            "messages": messages,
            "stream": True
        }
        if tools:
            payload["tools"] = tools

        response = requests.post(
            f"{config['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=60,
            stream=True
        )

        for line in response.iter_lines():
            if not line:
                continue
            line_text = line.decode('utf-8')
            if line_text.startswith("data: "):
                data = line_text[6:]
                if data == "[DONE]":
                    break
                try:
                    import json
                    chunk = json.loads(data)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})

                    # Handle content delta
                    content = delta.get("content", "")
                    if content:
                        full_content += content
                        if self._stream_callback:
                            self._stream_callback(content)

                    # Handle tool calls delta
                    tc_deltas = delta.get("tool_calls", [])
                    for tc_delta in tc_deltas:
                        index = tc_delta.get("index", 0)
                        while len(collected_tool_calls) <= index:
                            collected_tool_calls.append({"function": {}})
                        if "function" in tc_delta:
                            func_delta = tc_delta["function"]
                            if "name" in func_delta:
                                collected_tool_calls[index]["function"]["name"] = func_delta["name"]
                            if "arguments" in func_delta:
                                args = func_delta["arguments"]
                                if "arguments" not in collected_tool_calls[index]["function"]:
                                    collected_tool_calls[index]["function"]["arguments"] = ""
                                collected_tool_calls[index]["function"]["arguments"] += args

                except json.JSONDecodeError:
                    continue

        # Reconstruct tool calls in standard format
        tool_calls = []
        for tc in collected_tool_calls:
            if "function" in tc:
                tool_calls.append({
                    "id": f"call_{id(tc)}",
                    "function": tc["function"]
                })

        return {
            "choices": [{
                "message": {
                    "content": full_content,
                    "tool_calls": tool_calls if tool_calls else None
                }
            }]
        }

    def _process_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Process tool calls from LLM.

        Args:
            tool_calls: List of tool calls from LLM

        Returns:
            Tool execution results
        """
        results = []

        for tool_call in tool_calls:
            call_id = tool_call.get("id", f"call_{id(tool_call)}")
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            arguments = func.get("arguments", "{}")

            # Parse arguments if string
            if isinstance(arguments, str):
                import json
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    results.append({
                        "tool_call_id": call_id,
                        "output": "",
                        "error": f"Invalid arguments JSON: {arguments}"
                    })
                    continue

            # Execute tool
            output = self._execute_tool(tool_name, arguments)
            results.append({
                "tool_call_id": call_id,
                "output": output
            })

        return results

    def _execute_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """
        Execute a single tool with security intercept.

        Args:
            tool_name: Tool name
            tool_args: Tool arguments

        Returns:
            Tool execution result
        """
        # Security intercept
        allowed, error_msg = self._security.intercept(tool_name, tool_args, self.session_id)

        if not allowed:
            return f"Security blocked: {error_msg}"

        # Get tool from registry
        tool = self._registry.get(tool_name)

        if tool is None:
            return f"Tool '{tool_name}' not found"

        # Execute tool
        try:
            result = tool.execute(**tool_args)
            return str(result)
        except Exception as e:
            return f"Tool execution error: {str(e)}"

    def _get_all_tools_schema(self) -> List[Dict[str, Any]]:
        """
        Get schemas for all registered tools.

        Returns:
            List of tool schemas
        """
        return self._registry.get_all_schemas()
