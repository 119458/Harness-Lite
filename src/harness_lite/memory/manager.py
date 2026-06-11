"""Memory manager module.

Provides unified interface for managing both short-term JSON chat histories,
legacy Markdown auto-memories, and advanced Mem0 vector graph memories.
"""
import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional, Callable
from datetime import datetime
import threading

# 引入官方 OpenAI 同步客户端 (用于传统模式的自动蒸馏)
from openai import OpenAI
# 注意：mem0 为可选依赖。仅在 toggle_mem0() / _init_mem0() 中方引入。

from .store import MemoryStore
from harness_lite.config.loader import get_llm_config

logger = logging.getLogger("harness_lite.memory")


# 模块级失效回调列表：由 PromptBuilder 的 section 缓存等外部组件注册，
# 在 clear_context 后被依次调用以同步丢弃旧的渲染缓存
_invalidation_callbacks: List[Callable[[str], None]] = []
_invalidation_lock = threading.Lock()


def register_invalidation_callback(callback: Callable[[str], None]) -> None:
    """注册一个会在记忆失效（如 clear_context）时被回调的钩子。

    参数：
        callback: 接受一个 reason 字符串的可调用对象，返回值忽略。
    """
    with _invalidation_lock:
        if callback not in _invalidation_callbacks:
            _invalidation_callbacks.append(callback)


def _fire_invalidation(reason: str) -> None:
    """触发全部注册的失效回调，单个回调失败仅记日志。"""
    with _invalidation_lock:
        callbacks = list(_invalidation_callbacks)
    for cb in callbacks:
        try:
            cb(reason)
        except Exception as exc:
            logger.warning("失效回调执行异常（reason=%s）: %s", reason, exc)


class MemoryManager:
    """分层记忆管理器：统管短期 JSON 聊天流与双轨制长期记忆 (Markdown / Mem0)"""

    def __init__(self, store_dir: str = "./memory_store"):
        self.base_dir = Path(store_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._store = MemoryStore(store_dir=str(self.base_dir))

        self.global_pref_file = self.base_dir / "global_preferences.md"
        self._init_global_preferences()

        # 【新增状态】：默认关闭 Mem0，延迟初始化
        self.use_mem0 = False
        self.mem0 = None

    def _init_global_preferences(self) -> None:
        if not self.global_pref_file.exists():
            content = """# 全局用户开发偏好与习惯交互表 (Global Preferences)
- 在开始编写大规模核心代码前，优先输出一份简明的架构设计草案供用户确认。
- 保持回答和日志信息简明扼要，直接展示核心代码块，避免冗长的寒暄。
- 绝不在未经用户授权的情况下尝试读取沙箱外部的任何系统级敏感配置文件。
"""
            self.global_pref_file.write_text(content, encoding="utf-8")

    def _get_session_memory_paths(self, session_id: str) -> Tuple[Path, Path, Path]:
        """【原版保留】获取传统长期记忆文件的存放路径"""
        persistent_dir = self.base_dir / "persistent_memory"
        auto_mem_dir = persistent_dir / "auto_memory"
        auto_mem_dir.mkdir(parents=True, exist_ok=True)

        claude_md = persistent_dir / "CLAUDE.md"
        memory_md = auto_mem_dir / "MEMORY.md"

        if not claude_md.exists():
            claude_md.write_text("# 项目开发显式规范手册\n- 技术栈规范: 优先采用标准库与异步架构模式。\n",
                                 encoding="utf-8")
        if not memory_md.exists():
            memory_md.write_text(
                "# 智能体自主学习与动态纠错经验记忆库 (Auto-Memory)\n> 本文件记录用户人工驳回的教训与自愈准则。\n\n## 经过验证的行为准则与惩罚记忆：\n",
                encoding="utf-8")

        return persistent_dir, claude_md, memory_md

    def _init_mem0(self):
        """初始化 Mem0 引擎（延迟导入，避免未安装 mem0 时整个 Memory 模块崩溃）。"""
        try:
            from mem0 import Memory
        except ImportError as e:
            raise RuntimeError(
                "[Mem0 未安装] 请先 `pip install mem0ai` 并配置 embedding 模型 API 后再启用 /mem0。"
            ) from e

        config = get_llm_config()
        current_model = config.get("model_name", "gpt-3.5-turbo")

        # 模型降级路由
        mem0_model = current_model
        model_lower = current_model.lower()
        if "reasoner" in model_lower or "r1" in model_lower or "thinking" in model_lower:
            mem0_model = current_model.replace("reasoner", "chat").replace("-r1", "")
            if mem0_model == current_model:
                mem0_model = "deepseek-chat"
            logger.info(f"[Mem0 Init] 检测到思考模型，后台记忆蒸馏已降级路由至: {mem0_model}")

        mem0_config = {
            "vector_store": {
                "provider": "chroma",
                "config": {
                    "collection_name": "harness_auto_memory",
                    "path": str(self.base_dir / "mem0_db")
                }
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": mem0_model,
                    "api_key": config.get("api_key"),
                    "base_url": config.get("base_url"),
                    "temperature": 0.0,
                    "max_tokens": 1000,
                    "model_kwargs": {
                        "extra_body": {
                            "thinking": {"type": "disabled"},
                            "enable_thinking": False,
                            "chat_template_kwargs": {"thinking": False}
                        }
                    }
                }
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                    "api_key": config.get("api_key"),
                    "base_url": config.get("base_url")
                }
            }
        }
        return Memory.from_config(mem0_config)

    def toggle_mem0(self) -> str:
        """供 CLI 调用的切换开关"""
        if not self.use_mem0:
            # 开启前先尝试初始化，捕获 mem0 未安装/未配置的异常
            try:
                if self.mem0 is None:
                    self.mem0 = self._init_mem0()
            except RuntimeError as e:
                return f"[系统提示] ⚠️ Mem0 启用失败: {e}"
            except Exception as e:
                return f"[系统提示] ⚠️ Mem0 初始化异常: {e}"
            self.use_mem0 = True
            return "[系统提示] 🟢 已开启 Mem0 动态语义记忆模式 (向量检索)。"
        else:
            self.use_mem0 = False
            return "[系统提示] 🔴 已关闭 Mem0，切换回传统 Markdown 静态全量记忆模式。"

    # ==========================================
    # 接口一：短期 JSON 线性工作上下文操作
    # ==========================================

    def save_context(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        # 同步写入 JSON
        self._store.save(session_id, messages)

        # 仅在开启 Mem0 时，才执行后台向量化提取
        if self.use_mem0 and self.mem0:
            current_turn_messages = []
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    current_turn_messages = messages[i:]
                    break

            if current_turn_messages:
                def _background_mem0_add():
                    try:
                        self.mem0.add(current_turn_messages, user_id="global_admin")
                    except Exception as e:
                        logger.warning(f"后台 Mem0 记忆蒸馏失败 (不影响主流程): {str(e)}")

                threading.Thread(target=_background_mem0_add, daemon=True).start()

    def load_context(self, session_id: str) -> List[Dict[str, Any]]:
        return self._store.load(session_id)

    def trim_history(self, session_id: str, keep_last_n: int) -> None:
        messages = self._store.load(session_id)
        if len(messages) > keep_last_n:
            trimmed = messages[-keep_last_n:]
            self._store.save(session_id, trimmed)

    def clear_context(self, session_id: str) -> None:
        try:
            messages = self.load_context(session_id)
            if messages and messages[0].get("role") == "system":
                self.save_context(session_id, [messages[0]])
                logger.info(f"[Session-{session_id}] 成功执行内核级热重置，安全保留 System 设定。")
                _fire_invalidation("clear_context")
                return
        except Exception as e:
            logger.warning(f"[Session-{session_id}] 解析上下文尝试热重置时发生异常: {str(e)}")

        self._store.delete(session_id)
        session_dir = self.base_dir / "sessions" / f"session_{session_id}"
        if session_dir.exists():
            import shutil
            shutil.rmtree(session_dir)
        logger.info(f"[Session-{session_id}] 上下文为空或不合法，已完成基础物理清空。")
        _fire_invalidation("clear_context")

    @staticmethod
    def register_invalidation_callback(callback: Callable[[str], None]) -> None:
        """实例侧的转发入口，方便在持有 MemoryManager 时直接挂回调。"""
        register_invalidation_callback(callback)

    def list_sessions(self) -> List[str]:
        return [f.stem for f in self._store._store_dir.iterdir() if f.suffix == ".json"]

    # ==========================================
    # 接口二：高级 Markdown 长期记忆分层组装与注入
    # ==========================================

    def load_markdown_memories_as_text(self, session_id: str, current_task: Optional[str] = None) -> str:
        compiled_blocks = []
        _, claude_md, memory_md = self._get_session_memory_paths(session_id)

        # 1. 加载硬编码的全局极高优先级偏好
        if self.global_pref_file.exists():
            compiled_blocks.append(self.global_pref_file.read_text(encoding="utf-8"))

        # 2. 加载项目显式开发规范 (CLAUDE.md)
        if claude_md.exists():
            compiled_blocks.append(claude_md.read_text(encoding="utf-8"))

        # 3. 动态经验注入逻辑 (双分支)
        if self.use_mem0 and self.mem0 and current_task:
            # 开启状态：走 Mem0 动态语义检索
            try:
                results = self.mem0.search(query=current_task, user_id="global_admin", limit=5)
                if results:
                    mem_lines = ["## 从历史执行中动态提取的相关经验与自我约束："]
                    for res in results:
                        mem_text = res.get('memory', '') or res.get('text', '')
                        mem_lines.append(f"- {mem_text}")
                    compiled_blocks.append("\n".join(mem_lines))
            except Exception as e:
                logger.error(f"Mem0 语义检索异常: {str(e)}")
        else:
            # 关闭状态：走您原版代码的全局追加模式
            if memory_md.exists():
                compiled_blocks.append(memory_md.read_text(encoding="utf-8"))

        return "\n***\n".join(compiled_blocks)

    # ==========================================
    # 接口三：强行注入的纠错管线 (自主记忆蒸馏)
    # ==========================================

    def distill_and_record_correction(self, session_id: str, failed_command: str, correction_context: str) -> None:
        if self.use_mem0 and self.mem0:
            # 开启状态：直接调用 Mem0 处理
            try:
                correction_statement = (
                    f"【系统级纠错/人类禁令】在尝试执行 `{failed_command}` 操作时被阻断。 "
                    f"阻断原因及后续开发规范为: {correction_context}。"
                )
                self.mem0.add(correction_statement, user_id="global_admin")
                logger.info(f"[Session-{session_id}] 已通过 Mem0 成功固化纠错记忆: {correction_context}")
            except Exception as e:
                logger.error(f"向 Mem0 注入自愈记忆时发生异常: {str(e)}")
        else:
            # 关闭状态：完全恢复您原来基于 OpenAI API 提炼写入 Markdown 的逻辑
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
                client = OpenAI(
                    api_key=config["api_key"],
                    base_url=config["base_url"],
                    max_retries=3
                )

                response = client.chat.completions.create(
                    model=config["model_name"],
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2
                )

                distilled_rule = response.choices[0].message.content.strip()

                if distilled_rule.startswith("```"):
                    distilled_rule = distilled_rule.replace("```markdown", "").replace("```", "").strip()
                if not distilled_rule.startswith("-"):
                    distilled_rule = f"- {distilled_rule}"

                _, _, memory_md = self._get_session_memory_paths(session_id)
                current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
                final_line = f"{distilled_rule} (记录于 {current_date})\n"

                with open(memory_md, "a", encoding="utf-8") as f:
                    f.write(final_line)

                logger.info(f"[Session-{session_id}] 成功沉淀一条长效 Markdown 记忆: {distilled_rule}")

            except Exception as e:
                logger.error(f"传统长期记忆自主蒸馏管道发生异常: {str(e)}")