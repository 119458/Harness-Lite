"""
Loop engine module.

Core async LLM engine that provides infrastructure for strategies.
"""

from typing import Dict, Any, List, Optional, Callable
import httpx
import json
import asyncio

from harness_lite.memory.manager import MemoryManager
from harness_lite.registry import tool_registry
from harness_lite.registry import skill_registry
from harness_lite.security.manager import security_manager
from harness_lite.config.loader import get_llm_config
from harness_lite.loop.strategy import ReActStrategy



SYSTEM_PROMPT = """你是一个智能助手，可以使用工具来完成任务。

【可用物理工具】
{tools_schema}

【可用业务技能 / SOP 指南手册】
以下是你目前掌握的特定领域专业规范目录。如果你需要处理相关任务，请先调用 `read_skill` 工具查阅对应的详细规范手册：
{skills_list}

当你需要完成一个任务时：
1. 先检查该任务是否涉及上述业务技能。如果涉及，请先调用 `read_skill` 工具学习其详细 SOP 规范。
2. 如果可以直接回答，直接回复。
3. 如果需要调用外部物理工具，使用 tool_calls 格式。
4. 完成工具调用后，根据结果回复用户。

记住：
- 所有工具调用必须提供完整的参数
- 当请求多个独立信息时，可以一次性输出多个工具调用
"""

class AsyncLoopEngine:
    """Core Async LLM Engine."""
    def __init__(self, strategy=None):
        """
        Initialize the async engine.
        Args:
            strategy: 具体的执行策略，默认使用 ReActStrategy
        """
        self.strategy = strategy or ReActStrategy()
        self.memory = MemoryManager()
        self.security = security_manager
        self.registry = tool_registry

    async def run(self, task: str, session_id: str = "default", stream_callback: Callable[[str], None] = None, status_callback: Callable[[str], None] = None) -> str:
        """
        委托给具体的 Strategy 来执行任务。
        """
        return await self.strategy.execute(task, self, session_id, stream_callback, status_callback)

    def build_initial_messages(self, task: str) -> List[Dict[str, str]]:
        """
        构建初始系统消息
        """
        tools_schema = self._get_all_tools_schema()
        all_skills = skill_registry.list_all()
        if all_skills:
            lines = []
            for s in all_skills:
                s_name = s.name if hasattr(s, 'name') else s.get('name', '')
                s_desc = s.description if hasattr(s, 'description') else s.get('description', '')
                lines.append(f"- 技能名称: `{s_name}` | 简介: {s_desc}")
            skills_list_str = "\n".join(lines)
        else:
            skills_list_str = "当前未加载任何特定的业务技能指南。"
        system_content = SYSTEM_PROMPT.format(
            tools_schema=json.dumps(tools_schema, ensure_ascii=False),
            skills_list=skills_list_str
        )
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": task}
        ]

    async def call_llm_async(self, messages: List[Dict[str, Any]], stream: bool = False, stream_callback=None, status_callback=None) -> Dict[str, Any]:
        """
        异步调用 LLM API
        """
        config = get_llm_config()
        tools = self._get_all_tools_schema()
        async with httpx.AsyncClient(timeout=60.0) as client:
            if stream:
                return await self._call_llm_stream_async(client, config, messages, tools, stream_callback, status_callback)
            else:
                payload = {
                    "model": config["model_name"],
                    "messages": messages
                }
                if tools:
                    payload["tools"] = tools

                response = await client.post(
                    f"{config['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {config['api_key']}"},
                    json=payload
                )
                return response.json()

    async def _call_llm_stream_async(self, client, config, messages, tools, stream_callback, status_callback) -> Dict[str, Any]:
        """
        异步流式请求处理
        """
        full_content = ""
        collected_tool_calls = []
        notified_tool_call = False

        payload = {
            "model": config["model_name"],
            "messages": messages,
            "stream": True
        }
        if tools:
            payload["tools"] = tools
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
        async with client.stream(
                "POST",
                f"{config['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json"
                },
                content=payload_bytes
        ) as response:
            if response.status_code != 200:
                await response.aread()
                error_msg = f"\n[API 请求失败] 状态码: {response.status_code}, 详情: {response.text}\n"
                if stream_callback:
                    stream_callback(error_msg)
                return {"choices": [{"message": {"content": error_msg}}]}
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue

                data = line[6:]
                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")

                    if content:
                        full_content += content
                        if stream_callback:
                            stream_callback(content)

                    # 收集流式返回的 tool_calls
                    tc_deltas = delta.get("tool_calls", [])
                    if tc_deltas:
                        if not notified_tool_call:
                            if status_callback:
                                status_callback("[🧠 思考中] 模型决定调用外部能力，正在构造参数...")
                            notified_tool_call = True
                    for tc_delta in tc_deltas:
                        index = tc_delta.get("index", 0)
                        while len(collected_tool_calls) <= index:
                            collected_tool_calls.append({"function": {}})
                        if "id" in tc_delta:
                            collected_tool_calls[index]["id"] = tc_delta["id"]
                        if "function" in tc_delta:
                            func_delta = tc_delta["function"]
                            if "name" in func_delta:
                                collected_tool_calls[index]["function"]["name"] = func_delta["name"]
                            if "arguments" in func_delta:
                                if "arguments" not in collected_tool_calls[index]["function"]:
                                    collected_tool_calls[index]["function"]["arguments"] = ""
                                collected_tool_calls[index]["function"]["arguments"] += func_delta["arguments"]

                except json.JSONDecodeError:
                    continue

        tool_calls = []
        for tc in collected_tool_calls:
            if "function" in tc and tc["function"].get("name"):
                tool_calls.append({
                    "id": tc.get("id", f"call_{id(tc)}"),
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

    async def _safe_execute_tool_wrapper(self, call_id: str, tool_name: str, arguments: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """
        异步工具执行的包装器。
        使用 asyncio.to_thread 防止同步的工具代码阻塞主事件循环，
        并确保任何未知的极端异常都能被打包成返回结果，绝不导致框架崩溃。
        """
        try:
            output = await asyncio.to_thread(self._execute_tool, tool_name, arguments, session_id)
            return {"tool_call_id": call_id, "output": output}
        except Exception as e:
            return {"tool_call_id": call_id, "output": "", "error": f"[System Critical Error] 异步调度框架异常: {str(e)}"}

    async def process_tool_calls_async(self, tool_calls: List[Dict[str, Any]], session_id: str) -> List[Dict[str, Any]]:
        """
        异步处理工具调用，增强 JSON 容错与并发执行
        """
        results = []
        tasks = []
        valid_tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name")]
        for tool_call in valid_tool_calls:
            call_id = tool_call.get("id", f"call_{id(tool_call)}")
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            arguments = func.get("arguments", "{}")

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    error_msg = f"JSON解析失败: 无法解析参数 '{arguments}'。原因: {str(e)}。请检查是否缺失括号、引号转义是否正确，并重新输出合法的 JSON 参数。"
                    results.append({"tool_call_id": call_id, "output": "", "error": error_msg})
                    continue
            task = asyncio.create_task(
                self._safe_execute_tool_wrapper(call_id, tool_name, arguments, session_id)
            )
            tasks.append(task)
        if tasks:
            # 使用 return_exceptions=True 是并行的安全底线
            # 即使其中一个线程崩溃，其他任务的结果依然会被保留
            completed_results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in completed_results:
                if isinstance(res, Exception):
                    results.append({"tool_call_id": "unknown", "output": "", "error": f"Task Failed: {str(res)}"})
                else:
                    results.append(res)
        return results

    def _execute_tool(self, tool_name: str, tool_args: Dict[str, Any], session_id: str) -> str:
        """
        执行单一工具,增强异常反馈
        """
        allowed, error_msg = self.security.intercept(tool_name, tool_args, session_id)
        if not allowed:
            return f"[Security Blocked] 安全拦截: {error_msg}。请更换策略。"
        tool = self.registry.get(tool_name)
        if tool is None:
            return f"[Tool Not Found] 工具 '{tool_name}' 不存在，请检查并使用系统提供的工具名称。"

        try:
            # 兼容现有的同步 Tool execute，后续可扩展 await tool.execute_async()
            result = tool.execute(**tool_args)
            return str(result)
        except Exception as e:
            import traceback
            error_stack = traceback.format_exc().strip().split("\n")[-2:]
            stack_str = ' '.join(error_stack)
            return f"[Execution Error] 工具执行时抛出代码异常: {str(e)}。详细信息: {stack_str}。请根据报错信息更换参数或改用其他方法。"

    def _get_all_tools_schema(self) -> List[Dict[str, Any]]:
        return self.registry.get_all_schemas()
