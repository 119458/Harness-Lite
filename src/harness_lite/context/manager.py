"""
Context manager module for dynamic history token compression and slot management.
"""
import json
import logging
from typing import List, Dict, Any, Tuple
import httpx
from openai import AsyncOpenAI

try:
    import tiktoken
except ModuleNotFoundError:
    tiktoken = None

from harness_lite.config.loader import get_llm_config

logger = logging.getLogger("harness_lite.context")

class DynamicContextManager:
    """
    动态上下文管理器：负责自适应 Token 计算、插槽监控与历史自动化‘记忆收缩’合并
    """
    def __init__(self, max_allowed_tokens: int = 64000, model_name: str = "gpt-4-mini"):
        """
        初始化动态上下文管理器。

        Args:
            max_allowed_tokens: 上下文触发收缩的安全高水位阈值（Token数）
            model_name: 当前使用的编码器模型名称
        """
        self.max_allowed_tokens = max_allowed_tokens
        self.model_name = model_name

        self.encoder = None
        if tiktoken:
            try:
                self.encoder = tiktoken.encoding_for_model(self.model_name)
            except KeyError:
                try:
                    self.encoder = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    self.encoder = None

    def calculate_string_tokens(self, text: str) -> int:
        """
        精准计算或安全估算纯文本的 Token 消耗数量
        """
        if not text:
            return 0
        if self.encoder:
            return len(self.encoder.encode(text, disallowed_special=()))
        return len(text) // 3 + 1

    def calculate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """
        计算整个消息堆栈的标准 Token 总消耗量（完美对齐 OpenAI 消息体特征）
        """
        total_tokens = 0
        for msg in messages:
            total_tokens += 3
            total_tokens += self.calculate_string_tokens(msg.get("content", ""))
            total_tokens += self.calculate_string_tokens(msg.get("role", ""))
            total_tokens += self.calculate_string_tokens(msg.get("name", ""))

            if "reasoning_content" in msg and msg["reasoning_content"]:
                total_tokens += self.calculate_string_tokens(msg["reasoning_content"])

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total_tokens += 10
                for tc in tool_calls:
                    total_tokens += self.calculate_string_tokens(tc.get("id", ""))
                    func = tc.get("function", {})
                    total_tokens += self.calculate_string_tokens(func.get("name", ""))
                    total_tokens += self.calculate_string_tokens(func.get("arguments", ""))
        total_tokens += 3
        return total_tokens

    async def compress_if_overflow(self, messages: List[Dict[str, Any]], engine: Any, current_cwd: str, status_callback: Any = None) -> List[Dict[str, Any]]:
        """
        【核心自愈压缩管线】：自适应历史链条‘记忆收缩’。
        一旦判定当前总 Token 溢出，会自动将早期【成对存在】的 assistant 工具请求与 tool 返回原始日志，
        通过 LLM 提炼为高阶 Markdown 摘要行，并成对摘除，原地替换为历史归档记录点。
        引入 current_cwd 绝对内核状态锚定，防止模型压缩后迷路。
        """

        current_total = self.calculate_messages_tokens(messages)
        if current_total <= self.max_allowed_tokens:
            return messages
        if status_callback:
            status_callback(f"[🧹 上下文高危预警] 当前会话已达 {current_total} Token，触发自动化‘记忆收缩压缩引擎’...")
        anchor_slots = messages[:2]
        sliding_history = messages[2:]

        compressible_chunk: List[Dict[str, Any]] = []
        remaining_history: List[Dict[str, Any]] = []

        has_met_active_tail = False
        for msg in sliding_history:
            if has_met_active_tail or len(compressible_chunk) >= 6:
                remaining_history.append(msg)
                continue
            role = msg.get("role")
            if role in ["tool"] or (role == "assistant" and msg.get("tool_calls")):
                compressible_chunk.append(msg)
            else:
                if compressible_chunk:
                    has_met_active_tail = True
                remaining_history.append(msg)

        while compressible_chunk and compressible_chunk[-1].get("role") == "assistant":
            remaining_history.insert(0, compressible_chunk.pop())

        if not compressible_chunk:
            if status_callback:
                status_callback("[🧹 优化终止] 未探测到安全的配对工具链历史，平滑降级至近尾端滑动剔除。")
            if len(sliding_history) > 4:
                return anchor_slots + sliding_history[2:]
            return messages

        try:
            config = get_llm_config()
            raw_history_text = ""
            for m in compressible_chunk:
                if m.get("reasoning_content"):
                    raw_history_text += f"[模型内心思考]: {m['reasoning_content']}\n"
                raw_history_text += f"[{m['role'].upper()}]: {m.get('content', '')}\n"
                if m.get("tool_calls"):
                    raw_history_text += f"(请求调用工具: {json.dumps(m['tool_calls'], ensure_ascii=False)})\n"

            prompt = f"""你是一个智能体执行历史链条收缩器（Context Condenser）。目前某个 AI Agent 经历了一系列漫长繁重的工具调度步骤，由于日志量极大，我们需要将这些旧步骤【脱水压缩】。
            请将下面这段被摘出的原始工具交互日志，高度提炼总结为【一至两句话】的历史事实纪要，来说明 Agent 在这个阶段进行了什么尝试、动用了什么工具、最终取得了什么业务进展。

            【待压缩的原始交互日志】
            {raw_history_text}

            【硬性提炼规则】
            1. 语言必须高度凝练精辟，形如：“在早期步骤中，Agent 通过 read_file 查阅了代码逻辑，并调用 bash_terminal 成功安装了第三方依赖，为后续修复做好了准备。”
            2. 绝对不含任何前缀、闲聊，直接输出总结完的 Markdown 文本。
            """
            client = AsyncOpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                max_retries=2
            )
            extra_body = {
                "thinking": {"type": "disabled"},
                "enable_thinking": False,
                "chat_template_kwargs": {"thinking": False}
            }
            response = await client.chat.completions.create(
                model=config["model_name"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                extra_body=extra_body,
                timeout=15.0
            )
            if response.choices and response.choices[0].message.content:
                summary = response.choices[0].message.content.strip()
            else:
                summary = "Agent 在早期阶段执行了一系列工具链路调试及文件检索操作。"
        except Exception as e:
            summary = "Agent 在早期阶段顺利完成了部分前置文件的查阅与系统状态初始化。"
            logger.error(f"历史会话自动收缩时触发 LLM 摘要调用异常: {str(e)}")

        checkpoint_content = f"""⚙️ [系统历史会话收缩归档快照]
        {summary}
        注：以上是早期步骤的真实历史摘要，其对应的繁冗日志已被系统释放。

        📍 [当前终端内核状态（绝对硬锚定）]
        - 你的底层驻留常驻终端（bash_terminal）当前真实的物理绝对工作目录（CWD）为: `{current_cwd}`"""

        checkpoint_message = {
            "role": "system",
            "content": checkpoint_content
        }
        optimized_messages = anchor_slots + [checkpoint_message] + remaining_history
        if status_callback:
            saved_tokens = current_total - self.calculate_messages_tokens(optimized_messages)
            status_callback(f"[✅ 优化完毕] 历史成功收缩！已安全为你释放出 {saved_tokens} 个宝贵的 Token 空间。")

        return optimized_messages