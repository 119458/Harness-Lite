"""Memory manager module.

Provides unified interface for managing both short-term JSON chat histories
and Claude Code-style long-term Markdown auto-memories.
"""
import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime

# 【核心替换点】引入官方 OpenAI 同步客户端，斩断 httpx 依赖
from openai import OpenAI

from .store import MemoryStore
from harness_lite.config.loader import get_llm_config

logger = logging.getLogger("harness_lite.memory")


class MemoryManager:
    """分层记忆管理器：统管短期 JSON 聊天流与长期自愈型 Markdown 备忘录"""

    def __init__(self, store_dir: str = "./memory_store"):
        self.base_dir = Path(store_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._store = MemoryStore(store_dir=str(self.base_dir))

        self.global_pref_file = self.base_dir / "global_preferences.md"
        self._init_global_preferences()

    def _init_global_preferences(self) -> None:
        if not self.global_pref_file.exists():
            content = """# 全局用户开发偏好与习惯交互表 (Global Preferences)
- 在开始编写大规模核心代码前，优先输出一份简明的架构设计草案供用户确认。
- 保持回答和日志信息简明扼要，直接展示核心代码块，避免冗长的寒暄。
- 绝不在未经用户授权的情况下尝试读取沙箱外部的任何系统级敏感配置文件。
"""
            self.global_pref_file.write_text(content, encoding="utf-8")

    def _get_session_memory_paths(self, session_id: str) -> Tuple[Path, Path, Path]:
        """
        获取长期记忆文件的存放路径。
        解耦动态会话，开辟长效项目级永久累积区，支持跨会话历史共享一套进化备忘录。
        """
        persistent_dir = self.base_dir / "persistent_memory"
        auto_mem_dir = persistent_dir / "auto_memory"
        auto_mem_dir.mkdir(parents=True, exist_ok=True)

        claude_md = persistent_dir / "CLAUDE.md"
        memory_md = auto_mem_dir / "MEMORY.md"

        if not claude_md.exists():
            claude_md.write_text("# 项目开发显式规范手册\n- 技术栈规范: 优先采用标准库与异步架构模式。\n", encoding="utf-8")
        if not memory_md.exists():
            memory_md.write_text("# 智能体自主学习与动态纠错经验记忆库 (Auto-Memory)\n> 本文件记录用户人工驳回的教训与自愈准则。\n\n## 经过验证的行为准则与惩罚记忆：\n", encoding="utf-8")

        return persistent_dir, claude_md, memory_md

    # ==========================================
    # 接口一：短期 JSON 线性工作上下文操作 (向前兼容)
    # ==========================================

    def save_context(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        self._store.save(session_id, messages)

    def load_context(self, session_id: str) -> List[Dict[str, Any]]:
        return self._store.load(session_id)

    def trim_history(self, session_id: str, keep_last_n: int) -> None:
        messages = self._store.load(session_id)
        if len(messages) > keep_last_n:
            trimmed = messages[-keep_last_n:]
            self._store.save(session_id, trimmed)

    def clear_context(self, session_id: str) -> None:
        self._store.delete(session_id)
        session_dir = self.base_dir / "sessions" / f"session_{session_id}"
        if session_dir.exists():
            import shutil
            shutil.rmtree(session_dir)

    def list_sessions(self) -> List[str]:
        return [
            f.stem for f in self._store._store_dir.iterdir()
            if f.suffix == ".json"
        ]

    # ==========================================
    # 接口二：高级 Markdown 长期记忆分层组装与注入
    # ==========================================

    def load_markdown_memories_as_text(self, session_id: str) -> str:
        _, claude_md, memory_md = self._get_session_memory_paths(session_id)
        compiled_blocks = []

        if self.global_pref_file.exists():
            compiled_blocks.append(self.global_pref_file.read_text(encoding="utf-8"))
        if claude_md.exists():
            compiled_blocks.append(claude_md.read_text(encoding="utf-8"))
        if memory_md.exists():
            compiled_blocks.append(memory_md.read_text(encoding="utf-8"))

        return "\n***\n".join(compiled_blocks)

    # ==========================================
    # 接口三：自主记忆蒸馏管道 (Auto-Memory Distiller)
    # ==========================================

    def distill_and_record_correction(self, session_id: str, failed_command: str, correction_context: str) -> None:
        """
        【自主学习自愈核心】：将错误教训或 Layer 3 人工驳回原因，采用官方 OpenAI SDK 蒸馏并固化。
        """
        try:
            config = get_llm_config()
            if not config or not config.get("api_key"):
                logger.warning("未配置大模型凭证，跳过长期记忆自主蒸馏。")
                return

            prompt = f"""你是一个智能体高阶行为经验提炼器（Memory Distiller）。目前某个 AI Agent 在执行任务时触犯了安全、环境或业务逻辑边界，被人类用户/系统强制拦截驳回。
请深入剖析本次犯错场景，并为该 Agent 提炼出【一条】极度精炼、不含任何废话和前缀的 Markdown 列表形式的行为备忘准则（行为负向反馈），以警示它下次绝不再犯。

【犯错上下文】
- 触发报错的动作/指令: {failed_command}
- 被人类拒绝/拦截的精准原因: {correction_context}

【提炼要求】
1. 必须是单行 Markdown 列表项，形如: `- [纠错] 在处理XXXX时，严禁使用XXXX，必须通过XXXX来实现。`
2. 绝对不包含时间戳、绝对不带有任何闲聊或引言、字数锁死在 120 字符以内，直切痛点。

请直接输出这一行 Markdown 文本：
"""
            # 初始化官方 OpenAI 同步客户端，继承重试防线
            client = OpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"],
                max_retries=3
            )

            # 调用官方 API
            response = client.chat.completions.create(
                model=config["model_name"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2
            )

            distilled_rule = response.choices[0].message.content.strip()

            # 清洗包裹标记
            if distilled_rule.startswith("```"):
                distilled_rule = distilled_rule.replace("```markdown", "").replace("```", "").strip()
            if not distilled_rule.startswith("-"):
                distilled_rule = f"- {distilled_rule}"

            # 永久写入共享的长效记忆区
            _, _, memory_md = self._get_session_memory_paths(session_id)
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
            final_line = f"{distilled_rule} (记录于 {current_date})\n"

            with open(memory_md, "a", encoding="utf-8") as f:
                f.write(final_line)

            logger.info(f"[Session-{session_id}] 成功沉淀一条长效 Markdown 记忆: {distilled_rule}")

        except Exception as e:
            logger.error(f"长期记忆自主蒸馏管道发生异常: {str(e)}")