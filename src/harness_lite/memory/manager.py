"""Memory manager module.

提供短期 JSON 聊天上下文管理，并集成新一代基于文件的长期记忆系统。
"""
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List

from .store import MemoryStore

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
    """分层记忆管理器：短期 JSON 聊天流 + 文件型长期记忆系统。"""

    def __init__(self, store_dir: str = "./memory_store"):
        self.base_dir = Path(store_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._store = MemoryStore(store_dir=str(self.base_dir))

        # 延迟导入避免循环依赖（long_term.py 反向依赖 _fire_invalidation）
        from harness_lite.memory.long_term import LongTermMemoryManager
        self.long_term = LongTermMemoryManager(
            base_dir=str(self.base_dir / "long_term")
        )

    # ==========================================
    # 短期 JSON 线性工作上下文操作
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
        # 单 session 级失效：仅清当前 session 的长期记忆 read_set/counter，
        # 不能污染其他活跃 session 的已读去重状态
        try:
            self.long_term.clear_read_set(session_id)
        except Exception as exc:
            logger.warning(
                f"[Session-{session_id}] 清理长期记忆 read_set 异常: {exc}"
            )

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
            shutil.rmtree(session_dir)
        logger.info(f"[Session-{session_id}] 上下文为空或不合法，已完成基础物理清空。")
        _fire_invalidation("clear_context")

    @staticmethod
    def register_invalidation_callback(callback: Callable[[str], None]) -> None:
        """实例侧的转发入口，方便在持有 MemoryManager 时直接挂回调。"""
        register_invalidation_callback(callback)

    def list_sessions(self) -> List[str]:
        return [f.stem for f in self._store._store_dir.iterdir() if f.suffix == ".json"]
