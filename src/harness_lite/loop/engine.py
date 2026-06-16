"""
Loop engine module.

Fully patched industrial version utilizing the official OpenAI Python SDK
with advanced delta null-guards and surrogate sanitization to prevent all stream crashes.
"""
from random import choice
from typing import Dict, Any, List, Optional, Callable, AsyncGenerator
import json
import asyncio
import os
import platform as _platform_mod
from datetime import datetime
from pathlib import Path

# 引入官方 OpenAI SDK 核心库
from openai import AsyncOpenAI
import httpx

from harness_lite.memory.manager import MemoryManager
from harness_lite.registry import tool_registry
from harness_lite.registry import skill_registry
from harness_lite.security.manager import security_manager
from harness_lite.config.loader import get_main_config

# 物理执行层的会话上下文变量，打通全链路 Session 追踪
from harness_lite.tools.bash_terminal import current_session_id

# 阶段 A 引入的消息类型（暂作中间表示，B1 不改外部签名）
from harness_lite.loop.messages import StreamEvent

# 分层 system prompt 组装器
from harness_lite.prompt import PromptBuilder, PromptContext
from harness_lite.prompt.section_cache import get_default_cache


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
            config = get_main_config()
            self.strategy = ReActStrategy(
                max_tokens_threshold=config.get("max_context_tokens", 128_000),
                model_name=config.get("model_name", "gpt-4-mini"),
            )
        else:
            self.strategy = strategy

        self.memory = MemoryManager()
        self.security = security_manager
        self.registry = tool_registry

        # 进程级 section 缓存：静态前缀几乎永远命中，动态后缀按 dep_sig 自然失效
        self.cache = get_default_cache()

        # 一次性注册：当 MemoryManager.clear_context 被触发时同步清缓存，
        # 避免新会话仍然读到旧的 memory_recall 段
        try:
            self.memory.register_invalidation_callback(
                lambda reason: self.cache.clear(reason)
            )
        except AttributeError:
            # MemoryManager 未升级时降级为 no-op，不阻塞引擎启动
            pass

        # 注册 sidecar 失效回调：MemoryManager.clear_context 时同步清空 pipeline 状态
        try:
            pipeline = self.strategy.context_manager.pipeline
            self.memory.register_invalidation_callback(
                lambda reason: pipeline.reset_session(reason)
            )
        except AttributeError:
            pass

    async def run(self, task: str, session_id: str = "default", stream_callback: Callable[[str], None] = None,
                  status_callback: Callable[[str], None] = None) -> str:
        # 协程入口层：强制绑定当前异步上下文的 session_id 状态
        current_session_id.set(session_id)

        # B2 阶段：委托 QueryEngine 执行
        from harness_lite.loop.query_engine import QueryEngine

        engine = QueryEngine(engine=self, session_id=session_id)
        result = await engine._consume_to_result(
            stream_callback=stream_callback,
            status_callback=status_callback,
        )(task)

        return result.text

    def build_initial_messages(self, task: str, session_id: str = "default") -> List[Dict[str, str]]:
        """
        构建初始系统消息：通过 PromptBuilder 分层组装，再追加用户 task。

        所有运行时上下文（沙箱根、工具 schema、技能、长效记忆、环境元数据等）
        在此处一次性收齐，传入 PromptContext 后由各 section 自行渲染。
        """
        ctx = self._build_prompt_context(task, session_id)
        system_text = PromptBuilder(ctx, self.cache).build()

        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": task},
        ]

    def _build_prompt_context(self, task: str, session_id: str) -> PromptContext:
        """收集 PromptContext 所需的全部字段（IO/反射集中在此，section 保持纯函数）。"""
        tools_schema = self._get_all_tools_schema()
        tools_schema_json = json.dumps(tools_schema, ensure_ascii=False)
        enabled_tools = tuple(sorted(t.get("function", {}).get("name", "") for t in tools_schema))

        skills_list = self._collect_skills_list()
        sandbox_roots = tuple(sorted(str(r) for r in self.security.active_sandbox_roots))
        memory_text = self.memory.load_markdown_memories_as_text(session_id, current_task=task)

        try:
            # TODO(三模型差异化): 后续可切换为 get_small_config() / get_medium_config()
            config = get_main_config()
        except Exception:
            config = {}

        return PromptContext(
            task=task,
            session_id=session_id,
            model_name=config.get("model_name", "") if isinstance(config, dict) else "",
            sandbox_roots=sandbox_roots,
            enabled_tools=enabled_tools,
            tools_schema_json=tools_schema_json,
            skills_list=skills_list,
            memory_text=memory_text,
            mem0_enabled=bool(getattr(self.memory, "use_mem0", False)),
            cwd=self._safe_cwd(),
            is_git=self._detect_git(),
            platform=_platform_mod.system(),
            shell=os.environ.get("SHELL", ""),
            os_version=_platform_mod.platform(),
            current_date=datetime.now().strftime("%Y/%m/%d"),
            thinking_mode=bool(config.get("thinking_mode")) if isinstance(config, dict) else False,
        )

    @staticmethod
    def _collect_skills_list() -> tuple:
        """把 skill_registry 中的 Skill 实例归一化成 dict 列表。"""
        items = []
        for skill in skill_registry.list_all():
            name = getattr(skill, "name", "") if not isinstance(skill, dict) else skill.get("name", "")
            desc = (
                getattr(skill, "description", "")
                if not isinstance(skill, dict)
                else skill.get("description", "")
            )
            items.append({"name": name, "description": desc})
        return tuple(items)

    @staticmethod
    def _safe_cwd() -> str:
        try:
            return os.getcwd()
        except Exception:
            return ""

    @staticmethod
    def _detect_git() -> bool:
        try:
            p = Path(os.getcwd())
            while p != p.parent:
                if (p / ".git").exists():
                    return True
                p = p.parent
        except Exception:
            return False
        return False

    def build_hot_swapped_context(self, task: str, session_id: str = "default") -> List[Dict[str, Any]]:
        """
        组装当前回合的完整上下文。强制使用包含最新 Mem0 检索结果的 System 提示词
        替换掉历史 JSON 中固化的旧提示词，打破冻结陷阱。
        """

        history_messages = self.memory.load_context(session_id)

        dynamic_initial_msgs = self.build_initial_messages(task, session_id)

        if not history_messages:
            return dynamic_initial_msgs
        else:
            if history_messages[0].get("role") == "system":
                history_messages[0] = dynamic_initial_msgs[0]
            cleaned_history = [m for m in history_messages if not m.get("is_meta")]
            cleaned_history.append({
                "role": "user",
                "content": task
            })
            return cleaned_history


    async def call_llm_async(self, messages: List[Dict[str, Any]], stream: bool = False, stream_callback=None,
                             status_callback=None) -> Dict[str, Any]:
        """
        利用 OpenAI SDK 异步安全请求模型，内置 3 次自动指数退避重试管线
        """
        # TODO(三模型差异化): 后续可切换为 get_small_config() / get_medium_config()
        config = get_main_config()
        tools = self._get_all_tools_schema() or None

        processed_messages = []
        for msg in messages:
            clean_msg = {k: v for k, v in msg.items()}
            if not config.get("thinking_mode") and "reasoning_content" in clean_msg:
                del clean_msg["reasoning_content"]
            processed_messages.append(clean_msg)

        # 数据在灌入 SDK 前，清洗掉残余的非标准 Surrogate 乱码
        safe_messages = sanitize_surrogates(processed_messages)

        # L4 上下文投影：在发往 SDK 前剥离内部字段、合并连续 system 锚点
        try:
            pipeline = self.strategy.context_manager.pipeline
            safe_messages = pipeline.project_for_llm(
                safe_messages,
                thinking_mode=bool(config.get("thinking_mode", False)),
            )
        except AttributeError:
            # strategy 不是 ReActStrategy（如 mock 或单测）时降级跳过
            pass

        client = AsyncOpenAI(
            api_key=config["api_key"],
            base_url=config["base_url"],
            max_retries=3,
            http_client=httpx.AsyncClient(trust_env=False)
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

        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_msg = f"\n[API 核心错误] OpenAI SDK 基础通信崩溃. 详情: {str(e)}\n"
            if stream_callback:
                stream_callback(error_msg)
            return {"choices": [{"message": {"content": error_msg}}]}
        finally:
            # 修复隐患：AsyncOpenAI 客户端用完即关，避免长会话连接数泄漏
            try:
                await client.close()
            except Exception:
                pass

    async def _call_llm_stream_async(self, client: AsyncOpenAI, config: Dict[str, Any], messages: List[Dict[str, Any]],
                                     tools: Optional[List[Any]], stream_callback, status_callback) -> Dict[str, Any]:
        """
        【B1 适配层】消费 `_stream_llm_events` 生成器并聚合为旧版 dict 返回值。
        外部签名与返回结构与 B1 之前完全一致，保证 strategy 零改动。
        """
        full_content = ""
        full_reasoning_content = ""
        tool_calls_list: List[Dict[str, Any]] = []
        final_finish_reason = "stop"

        try:
            async for event in self._stream_llm_events(client, config, messages, tools, status_callback):
                etype = event.type
                data = event.data

                if etype == "message_delta":
                    if "content" in data:
                        chunk_text = data["content"]
                        full_content += chunk_text
                        if stream_callback:
                            stream_callback(chunk_text)
                    if "reasoning_content" in data:
                        full_reasoning_content += data["reasoning_content"]
                        # status_callback 已在生成器内部处理思考流推送，这里仅累计

                elif etype == "message_stop":
                    final_finish_reason = data.get("finish_reason", "stop")
                    final_tool_calls = data.get("tool_calls")
                    if final_tool_calls:
                        tool_calls_list = final_tool_calls

                elif etype == "api_error":
                    # 生成器内部已 yield 过 error 事件；这里走兜底字符串路径
                    error_msg = data.get("message", "[未知 API 错误]")
                    if stream_callback:
                        stream_callback(error_msg)
                    return {"choices": [{"message": {"content": error_msg}, "finish_reason": "error"}]}

                # message_start 仅供 usage 统计，B1 暂不消费

            res_message: Dict[str, Any] = {
                "content": full_content,
                "tool_calls": tool_calls_list if tool_calls_list else None,
            }
            if config.get("thinking_mode") and full_reasoning_content:
                res_message["reasoning_content"] = full_reasoning_content

            return {"choices": [{"message": res_message, "finish_reason": final_finish_reason}]}

        except asyncio.CancelledError:
            # 上层取消必须重抛，不能吞掉
            raise
        except Exception as e:
            error_msg = f"\n[API 流式错误] SDK 传输流遭遇未知中断. 详情: {str(e)}\n"
            if stream_callback:
                stream_callback(error_msg)
            return {"choices": [{"message": {"content": error_msg}, "finish_reason": "error"}]}

    async def _stream_llm_events(
        self,
        client: AsyncOpenAI,
        config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]],
        status_callback,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        【B1 协议层】将 OpenAI 流式响应转化为结构化 StreamEvent 序列。

        事件类型：
        - message_start: 开始接收（携带 model 信息）
        - message_delta: 增量 chunk（content / reasoning_content / tool_call 累加片段）
        - message_stop:  本次流结束（携带 finish_reason 与最终聚合的 tool_calls）
        - api_error:     SDK 通信异常（携带 message）

        注意：
        1. tool_calls 的增量在内部状态机累加，仅在 message_stop 时一次性以完整结构 yield，
           避免下游消费方反复合并不完整的 chunk。
        2. status_callback 在思考流（reasoning_content）下仍由本函数推送，保持现有 UI 行为不变。
        3. 整个过程内部不调用 stream_callback；由上层（_call_llm_stream_async 或 query_engine）
           根据 message_delta 自行决定 UI 反馈。
        """
        is_thinking = config.get("thinking_mode", False)
        extra_body = {
            "thinking": {"type": "enabled" if is_thinking else "disabled"},
            "enable_thinking": is_thinking,
            "chat_template_kwargs": {"thinking": is_thinking},
        }

        kwargs = {
            "model": config["model_name"],
            "messages": messages,
            "stream": True,
            "extra_body": extra_body,
        }
        if tools:
            kwargs["tools"] = tools

        tool_calls_dict: Dict[int, Dict[str, Any]] = {}
        notified_tool_call = False
        final_finish_reason = "stop"
        started = False

        try:
            response_stream = await client.chat.completions.create(**kwargs)

            async for chunk in response_stream:
                if not started:
                    started = True
                    yield StreamEvent(type="message_start", data={"model": config.get("model_name")})

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # 拦截第三方网关空 delta（核心防御）
                if delta is None:
                    continue

                if choice.finish_reason:
                    final_finish_reason = choice.finish_reason

                # 思维链增量
                if is_thinking:
                    reasoning_chunk = getattr(delta, "reasoning_content", None)
                    if reasoning_chunk:
                        if status_callback:
                            clean_reasoning = reasoning_chunk.replace("\n", " ").strip()
                            if clean_reasoning:
                                status_callback(f"[🧠 思考中] {clean_reasoning}")
                        yield StreamEvent(
                            type="message_delta",
                            data={"reasoning_content": reasoning_chunk},
                        )

                # 文本增量
                if hasattr(delta, "content") and delta.content:
                    yield StreamEvent(
                        type="message_delta",
                        data={"content": delta.content},
                    )

                # 工具调用增量（内部累加，message_stop 时一次性吐完整结构）
                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    if not notified_tool_call and status_callback:
                        status_callback("[🧠 思考中] 模型决定调用外部能力，正在构造参数...")
                        notified_tool_call = True

                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": tc_chunk.id or f"call_{id(tc_chunk)}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] = tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                tool_calls_dict[idx]["function"]["name"] = tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments

            final_tool_calls = list(tool_calls_dict.values()) if tool_calls_dict else None
            yield StreamEvent(
                type="message_stop",
                data={"finish_reason": final_finish_reason, "tool_calls": final_tool_calls},
            )

        except asyncio.CancelledError:
            # 必须重抛，让上层 finally 闭合资源
            raise
        except Exception as e:
            yield StreamEvent(
                type="api_error",
                data={"message": f"\n[API 流式错误] SDK 传输流遭遇未知中断. 详情: {str(e)}\n"},
            )

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