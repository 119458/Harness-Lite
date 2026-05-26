"""
Loop engine module.

Core async LLM engine that provides infrastructure for strategies.
"""
import os
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

# 【核心添加点】引入物理执行层的会话上下文变量，打通全链路 Session 追踪
from harness_lite.tools.execution_ops import current_session_id

SYSTEM_PROMPT = """你是一个智能助手，可以使用工具来完成复杂的系统任务。

【环境与沙箱状态】
- 当前工作区（沙箱）绝对路径: {workspace_root}
- 安全限制: 你的所有文件读取、创建、编辑以及终端操作，都必须严格限制在上述工作区范围内。系统已开启沙箱拦截，任何试图访问该目录之外（如 /etc, ~/.ssh 等）的操作都会被强制拒绝。
- 路径建议: 在调用文件工具（如 create_file, edit_file）或终端执行时，请优先使用**相对于当前工作区的相对路径**（例如直接使用 `src/main.py` 而不是绝对路径）。

【可用物理工具】
{tools_schema}
当使用 edit_file 时，请必须先使用 read_file 查阅目标文件的具体行号，然后精确提供 start_line 和 end_line 进行局部替换。

【可用业务技能 / SOP 指南手册】
以下是你目前掌握的特定领域专业规范目录。如果你需要处理相关任务，请先调用 `read_skill` 工具查阅对应的详细规范手册：
{skills_list}

当你需要完成一个任务时：
1. 先检查该任务是否涉及上述业务技能。如果涉及，请先调用 `read_skill` 工具学习其详细 SOP 规范。
2. 明确你要操作的文件路径，确保它在工作区沙箱内。
3. 如果可以直接回答，直接回复；如果需要调用外部物理工具，使用 tool_calls 格式。
4. 完成工具调用后，根据结果回复用户。
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

    async def run(self, task: str, session_id: str = "default", stream_callback: Callable[[str], None] = None,
                  status_callback: Callable[[str], None] = None) -> str:
        """
        委托给具体的 Strategy 来执行任务。
        """
        # 【核心添加点】协程入口层：强制绑定当前异步上下文的 session_id 状态
        current_session_id.set(session_id)
        return await self.strategy.execute(task, self, session_id, stream_callback, status_callback)

    def build_initial_messages(self, task: str, session_id: str = "default") -> List[Dict[str, str]]:
        """
        构建初始系统消息（已升级：支持 Session 级别物理沙箱路径的自适应渲染）
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

        prior_memories_md = self.memory.load_markdown_memories_as_text(session_id)

        # 【核心修改点】联动安全管理器，实时获取该租户独占的物理子文件夹，作为注入提示词的绝对路径边界
        sandbox_absolute_path = str(self.security.get_session_workspace(session_id))

        system_content = SYSTEM_PROMPT.format(
            workspace_root=sandbox_absolute_path,
            tools_schema=json.dumps(tools_schema, ensure_ascii=False),
            skills_list=skills_list_str
        )
        full_system_prompt = f"{system_content}\n\n【你先前通过学习或被人类纠错沉淀下来的核心长效行为备忘录】\n{prior_memories_md}"
        return [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": task}
        ]

    async def call_llm_async(self, messages: List[Dict[str, Any]], stream: bool = False, stream_callback=None,
                             status_callback=None) -> Dict[str, Any]:
        """
        异步调用 LLM API 带有指数退避的重试机制 (Exponential Backoff Retry)
        """
        config = get_llm_config()
        tools = self._get_all_tools_schema()
        max_retries = 3
        base_wait = 2
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    if stream:
                        return await self._call_llm_stream_async(client, config, messages, tools, stream_callback,
                                                                 status_callback)
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
                        response.raise_for_status()
                        return response.json()
            except(httpx.RequestError, httpx.HTTPStatusError) as e:
                if attempt == max_retries - 1:
                    error_msg = f"\n[API 致命错误] 网络请求失败且重试耗尽。状态: {str(e)}\n"
                    if stream_callback:
                        stream_callback(error_msg)
                    return {"choices": [{"message": {"content": error_msg}}]}
                wait_time = base_wait * (2 ** attempt)
                if stream_callback:
                    status_callback(
                        f"[🔁 网络抖动] API 调用异常 ({str(e)})，将在 {wait_time} 秒后进行第 {attempt + 1} 次重试...")
                await asyncio.sleep(wait_time)

    async def _call_llm_stream_async(self, client, config, messages, tools, stream_callback, status_callback) -> Dict[
        str, Any]:
        """
        异步流式请求处理 (增加抛出异常以便外层进行重试)
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
            response.raise_for_status()
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

        def sanitize_text(text: str) -> str:
            if not text:
                return text
            try:
                text = text.encode("utf-16", "surrogatepass").decode('utf-16')
                text = text.encode('utf-8', 'replace').decode('utf-8')
            except Exception:
                pass
            return text

        full_content = sanitize_text(full_content)

        tool_calls = []
        for tc in collected_tool_calls:
            if "function" in tc and tc["function"].get("name"):
                args = tc["function"].get("arguments", "")
                tc["function"]["arguments"] = sanitize_text(args)
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

    async def _safe_execute_tool_wrapper(self, call_id: str, tool_name: str, arguments: Dict[str, Any],
                                         session_id: str) -> Dict[str, Any]:
        """
        异步工具执行的包装器。
        使用 asyncio.to_thread 防止同步的工具代码阻塞主事件循环
        """
        try:
            output = await asyncio.to_thread(self._execute_tool, tool_name, arguments, session_id)
            return {"tool_call_id": call_id, "output": output}
        except Exception as e:
            return {"tool_call_id": call_id, "output": "",
                    "error": f"[System Critical Error] 异步调度框架异常: {str(e)}"}

    async def process_tool_calls_async(self, tool_calls: List[Dict[str, Any]], session_id: str) -> List[Dict[str, Any]]:
        """
        异步处理工具调用：采用严格顺序执行与 Fail-Fast (快速失败) 策略。
        """
        results = []
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
                    error_msg = f"JSON解析失败: 无法解析参数 '{arguments}'。原因: {str(e)}。请检查并重新输出合法的 JSON 参数。"
                    results.append({"tool_call_id": call_id, "output": "", "error": error_msg})
                    continue

            res = await self._safe_execute_tool_wrapper(call_id, tool_name, arguments, session_id)
            results.append(res)
            if res.get("error") or str(res.get("output", "")).startswith(
                    ("[Security", "[Error", "Failed", "静态防御", "语义审计")):
                results.append({
                    "tool_call_id": "system_interrupt",
                    "output": "",
                    "error": f"[System] 检测到前置工具 '{tool_name}' 未能安全通过防御审计，出于逻辑依赖与系统安全考虑，同批次后续指令已被取消。"
                })
                break
        return results

    def _execute_tool(self, tool_name: str, tool_args: Dict[str, Any], session_id: str) -> str:
        """
        执行单一工具，增强异常反馈并实施进程池会话双重绑定机制
        """
        # 【核心添加点】双重保险：在物理线程池的工作线程上下文中，强制注入当前的 session_id
        current_session_id.set(session_id)

        allowed, error_msg = self.security.intercept(tool_name, tool_args, session_id)
        if not allowed:
            return f"[Security Blocked] 安全拦截: {error_msg}。请更换策略。"
        tool = self.registry.get(tool_name)
        if tool is None:
            return f"[Tool Not Found] 工具 '{tool_name}' 不存在，请检查并使用系统提供的工具名称。"

        try:
            result = tool.execute(**tool_args)
            return str(result)
        except Exception as e:
            import traceback
            error_stack = traceback.format_exc().strip().split("\n")[-2:]
            stack_str = ' '.join(error_stack)
            return f"[Execution Error] 工具执行时抛出代码异常: {str(e)}。详细信息: {stack_str}。请根据报错信息更换参数或改用其他方法。"

    def _get_all_tools_schema(self) -> List[Dict[str, Any]]:
        return self.registry.get_all_schemas()