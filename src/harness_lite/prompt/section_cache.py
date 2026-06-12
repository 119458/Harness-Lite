"""
按 (section 名, 依赖签名) 缓存渲染结果的轻量 LRU。

设计要点：
- 进程级单例：避免在多次 build_initial_messages 调用之间反复重算
- 容量上限：默认 32 条，section 数量 * 历史签名数 通常不会大
- 命中失效：上层只需调用 clear()，无需感知内部数据结构
- 线程安全：用 RLock 包住 dict 读写，应对多协程并发
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("harness_lite.prompt.cache")

_CAPACITY_DEFAULT = 32


class SectionCache:
    """轻量 LRU：键由 (section 名 + 依赖签名) 拼成，值为渲染后的文本或 None。"""

    def __init__(self, capacity: int = _CAPACITY_DEFAULT) -> None:
        self._capacity = max(1, capacity)
        self._store: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._lock = threading.RLock()

    def get(self, name: str, dep_sig: str) -> Optional[str]:
        """返回缓存的渲染结果；未命中返回 None，无法区分"命中但值为 None"的情况。

        约定：上层 PromptBuilder 不应缓存 None 值产生的"命中"——
        当前实现中 None 也会被存入，但 get 返回 None 时一律走重算路径，
        这对单段空内容的成本可以接受，换取调用方代码简洁。
        """
        key = self._make_key(name, dep_sig)
        with self._lock:
            if key not in self._store:
                return None
            # LRU：访问后挪到尾部
            value = self._store.pop(key)
            self._store[key] = value
            return value

    def put(self, name: str, dep_sig: str, value: Optional[str]) -> None:
        """写入缓存，超过容量时丢弃最早的条目。"""
        key = self._make_key(name, dep_sig)
        with self._lock:
            if key in self._store:
                self._store.pop(key)
            self._store[key] = value
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def clear(self, reason: str = "manual") -> None:
        """清空全部缓存。供 /clear、上下文压缩等场景调用。"""
        with self._lock:
            self._store.clear()
        logger.info("SectionCache 已清空，原因：%s", reason)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    @staticmethod
    def _make_key(name: str, dep_sig: str) -> str:
        return f"{name}::{dep_sig}"


# 进程级单例：engine 通过 get_default_cache() 取用
_default_cache: Optional[SectionCache] = None
_default_cache_lock = threading.RLock()


def get_default_cache() -> SectionCache:
    """获取进程级默认缓存。首次调用时创建。"""
    global _default_cache
    if _default_cache is not None:
        return _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = SectionCache()
    return _default_cache
