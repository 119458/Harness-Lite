"""
Loop engine module.

Fully patched industrial version utilizing the official OpenAI Python SDK
with advanced delta null-guards and surrogate sanitization to prevent all stream crashes.
"""
import os
from typing import Dict, Any, List, Optional, Callable
import json
import asyncio

# 引入官方 OpenAI SDK 核心库
from openai import AsyncOpenAI

from harness_lite.memory.manager import MemoryManager
from harness_lite.registry import tool_registry
from harness_lite.registry import skill_registry
from harness_lite.security.manager import security_manager
from harness_lite.config.loader import get_llm_config

# 物理执行层的会话上下文变量，打通全链路 Session 追踪
from harness_lite.tools.execution_ops import current_session_id

SYSTEM_PROMPT = """你是一个智能助手，可以使用工具来完成复杂的系统任务。

【环境与沙箱状态】
- 当前工作区（沙箱）绝对路径: {workspace_root}
- 安全限制: 你的所有文件读取、创建、编辑以及终端操作，都必须严格限制在上述工作区范围内。系统已开启沙箱拦截，任何试图访问该目录之外（如 /etc, ~/.ssh 等）的操作都会被强制拒绝。
- 路径建议: 在调用文件工具（如 create_file, edit_file）或终端执行时，请优先使用**相对于当前工作区的相对路径**（例如直接使用 `src/main.py` ）。

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


def sanitize_surrogates(data: Any) -> Any:
    """
    【核心自愈防线】深度递归清洗结构化数据中所有不合法的孤立代理对(Surrogates)字符。
    百分之百杜绝官方 OpenAI SDK 发生传输流底层编码熔断。
    """
    if isinstance(data, str):
        try:
            data.encode("utf-8")
            return data
        except UnicodeEncodeError:
            return "".join(c for c in data if not (0xD800 <= ord(c) <= 0xDFFF))
    elif isinstance(data, list):
        return [sanitize_surrogates(item) for item in data]
    elif isinstance(data, dict):
        return {k: sanitize_surrogates(v) for k, v in data.items()}
    return data


class AsyncLoopEngine:
    """Core Async LLM Engine powered by OpenAI Python SDK with multi-layer resilience."""

    def __init__(self, strategy=None):
        # ========================================================
        # 【延迟局部导入】完美斩断与 strategy 之间的循环引用，消灭 NameError
        # ========================================================
        if strategy is None:
            from harness_lite.loop.strategy import ReActStrategy
            self.strategy = ReActStrategy()
        else:
            self.strategy = strategy

        self.memory = MemoryManager()
        self.security = security_manager
        self.registry = tool_registry

    async def run(self, task: str, session_id: str = "default", stream_callback: Callable[[str], None] = None,
                  status_callback: Callable[[str], None] = None) -> str:
        # 协程入口层：强制绑定当前异步上下文的 session_id 状态
        current_session_id.set(session_id)
        return await self.strategy.execute(task, self, session_id, stream_callback, status_callback)

    def build_initial_messages(self, task: str, session_id: str = "default") -> List[Dict[str, str]]:
        """
        构建初始系统消息（支持 Session 级别物理沙箱路径的自适应渲染与 Claude Code 级分层记忆注入）
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

        # 联动安全管理器，实时获取该租户独占的物理子文件夹
        sandbox_absolute_path = str(self.security.get_session_workspace(session_id))

        system_content = SYSTEM_PROMPT.format(
            workspace_root=sandbox_absolute_path,
            tools_schema=json.dumps(tools_schema, ensure_ascii=False),
            skills_list=skills_list_str
        )

        prior_memories_md = self.memory.load_markdown_memories_as_text(session_id)
        full_system_prompt = f"{system_content}\n\n【你先前通过学习或被人类纠错沉淀下来的核心长效行为备忘录】\n{prior_memories_md}"

        return [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": task}
        ]

    async def call_llm_async(self, messages: List[Dict[str, Any]], stream: bool = False, stream_callback=None,
                             status_callback=None) -> Dict[str, Any]:
        """
        利用 OpenAI SDK 异步安全请求模型，内置 3 次自动指数退避重试管线
        """
        config = get_llm_config()
        tools = self._get_all_tools_schema() or None

        processed_messages = []
        for msg in messages:
            clean_msg = {k: v for k, v in msg.items()}
            if not config.get("thinking_mode") and "reasoning_content" in clean_msg:
                del clean_msg["reasoning_content"]
            processed_messages.append(clean_msg)

        # 数据在灌入 SDK 前，清洗掉残余的非标准 Surrogate 乱码
        safe_messages = sanitize_surrogates(processed_messages)

        client = AsyncOpenAI(
            api_key=config["api_key"],
            base_url=config["base_url"],
            max_retries=3
        )

        try:
            if stream:
                return await self._call_llm_stream_async(client, config, safe_messages, tools, stream_callback,
                                                         status_callback)
            else:
                is_thinking = config.get("thinking_mode", False)
                extra_body = {
                    "thinking": {"type": "enabled" if is_thinking else "disabled"},  # DeepSeek 官方原生标准协议
                    "enable_thinking": is_thinking,  # 阿里云 DashScope / Qwen 混合思考系列标准
                    "chat_template_kwargs": {"thinking": is_thinking}
                }
                kwargs = {
                    "model": config["model_name"],
                    "messages": safe_messages,
                    "extra_body": extra_body
                }
                if tools:
                    kwargs["tools"] = tools

                response = await client.chat.completions.create(**kwargs)
                return response.model_dump()

        except Exception as e:
            error_msg = f"\n[API 核心错误] OpenAI SDK 基础通信崩溃. 详情: {str(e)}\n"
            if stream_callback:
                stream_callback(error_msg)
            return {"choices": [{"message": {"content": error_msg}}]}

    async def _call_llm_stream_async(self, client: AsyncOpenAI, config: Dict[str, Any], messages: List[Dict[str, Any]],
                                     tools: Optional[List[Any]], stream_callback, status_callback) -> Dict[str, Any]:
        """
        利用 OpenAI SDK 实现结构化流式消息处理，带有点对点强制判空安全网
        """
        full_content = ""
        full_reasoning_content = ""
        tool_calls_dict = {}
        notified_tool_call = False

        is_thinking = config.get("thinking_mode", False)
        extra_body = {
            "thinking": {"type": "enabled" if is_thinking else "disabled"},  # DeepSeek 官方原生标准协议
            "enable_thinking": is_thinking,  # 阿里云 DashScope / Qwen 混合思考系列标准
            "chat_template_kwargs": {"thinking": is_thinking}  # vLLM 开源部署自适应模板指令标准
        }

        kwargs = {
            "model": config["model_name"],
            "messages": messages,
            "stream": True,
            "extra_body": extra_body
        }
        if tools:
            kwargs["tools"] = tools

        try:
            response_stream = await client.chat.completions.create(**kwargs)

            async for chunk in response_stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # ========================================================
                # 【🔥 核心修复防线】极其严密地拦截并跳过第三方网关下发的恶意空 delta 对象，
                # 彻底解决 'NoneType' object has no attribute 'content' 崩溃！
                # ========================================================
                if delta is None:
                    continue

                if config.get("thinking_mode"):
                    reasoning_chunk = getattr(delta, "reasoning_content", None)
                    if reasoning_chunk:
                        full_reasoning_content += reasoning_chunk
                        if status_callback:
                            clean_reasoning = reasoning_chunk.replace("\n", " ").strip()
                            if clean_reasoning:
                                status_callback(f"[🧠 思考中] {clean_reasoning}")

                # 1. 安全流式抽取纯文本
                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content
                    if stream_callback:
                        stream_callback(delta.content)

                # 2. 安全流式结构化累加工具参数
                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    if not notified_tool_call and status_callback:
                        status_callback("[🧠 思考中] 模型决定调用外部能力，正在构造参数...")
                        notified_tool_call = True

                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": tc_chunk.id or f"call_{id(tc_chunk)}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            }
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] = tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                tool_calls_dict[idx]["function"]["name"] = tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

            final_tool_calls = list(tool_calls_dict.values()) if tool_calls_dict else None

            res_message = {
                "content": full_content,
                "tool_calls": final_tool_calls
            }
            if config.get("thinking_mode") and full_reasoning_content:
                res_message["reasoning_content"] = full_reasoning_content

            return {"choices": [{"message": res_message}]}

        except Exception as e:
            error_msg = f"\n[API 流式错误] SDK 传输流遭遇未知中断. 详情: {str(e)}\n"
            if stream_callback:
                stream_callback(error_msg)
            return {"choices": [{"message": {"content": error_msg}}]}

    async def _safe_execute_tool_wrapper(self, call_id: str, tool_name: str, arguments: Dict[str, Any],
                                         session_id: str) -> Dict[str, Any]:
        try:
            output = await asyncio.to_thread(self._execute_tool, tool_name, arguments, session_id)
            return {"tool_call_id": call_id, "output": output}
        except Exception as e:
            return {"tool_call_id": call_id, "output": "",
                    "error": f"[System Critical Error] 异步调度思考异常: {str(e)}"}

    async def process_tool_calls_async(self, tool_calls: List[Dict[str, Any]], session_id: str) -> List[Dict[str, Any]]:
        """
        严格按顺序串行执行，配合 OpenAI 合法协议完成 Fail-Fast 阻断响应
        """
        results = []
        valid_tool_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name")]

        should_cancel_subsequent = False
        cancellation_reason = ""

        for tool_call in valid_tool_calls:
            call_id = tool_call.get("id", f"call_{id(tool_call)}")
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            arguments = func.get("arguments", "{}")

            if should_cancel_subsequent:
                results.append({
                    "tool_call_id": call_id,
                    "output": f"[Cancelled] 由于同批次前置依赖执行异常，此后续操作已被自动取消。原因: {cancellation_reason}",
                    "error": "[System Interrupt] 前置依赖阻断。"
                })
                continue

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    error_msg = f"JSON解析失败: 无法解析参数 '{arguments}'。原因: {str(e)}。"
                    results.append({"tool_call_id": call_id, "output": "", "error": error_msg})
                    should_cancel_subsequent = True
                    cancellation_reason = f"工具 '{tool_name}' 参数 JSON 结构损坏。"
                    continue

            res = await self._safe_execute_tool_wrapper(call_id, tool_name, arguments, session_id)
            results.append(res)

            output_val = res.get("output", "")
            if res.get("error") or str(output_val).startswith(
                    ("[Security", "[Error", "Failed", "静态防御", "语义审计")):
                should_cancel_subsequent = True
                cancellation_reason = f"前置工具 '{tool_name}' 未能安全通过防御审计或遭遇代码运行异常。"

        return results

    def _execute_tool(self, tool_name: str, tool_args: Dict[str, Any], session_id: str) -> str:
        current_session_id.set(session_id)

        allowed, error_msg = self.security.intercept(tool_name, tool_args, session_id)
        if not allowed:
            return f"[Security Blocked] 安全拦截: {error_msg}。请更换策略。"
        tool = self.registry.get(tool_name)
        if tool is None:
            return f"[Tool Not Found] 工具 '{tool_name}' 不存在。"

        try:
            result = tool.execute(**tool_args)
            return str(result)
        except Exception as e:
            import traceback
            error_stack = traceback.format_exc().strip().split("\n")[-2:]
            stack_str = ' '.join(error_stack)
            return f"[Execution Error] 工具执行时抛出代码异常: {str(e)}。详细信息: {stack_str}。"

    def _get_all_tools_schema(self) -> List[Dict[str, Any]]:
        return self.registry.get_all_schemas()