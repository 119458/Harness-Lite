"""
工具配额与 URL 黑名单的统一管理模块。

为新增的 4 个工具（fuzzy_edit / doc_fetch / task_scheduler / browser_automation）
提供：
1. 工具粒度的配额配置（并发数、文件大小、任务数等）；
2. 统一的危险 URL 黑名单（防 SSRF、防本地资源逃逸）；
3. 线程安全的并发计数器（仅内存级，不持久化）。
"""

import re
import threading
from typing import Dict, Tuple


# ---------------------------------------------------------------------------
# 工具配额配置：保持为模块级常量便于覆盖与单元测试
# ---------------------------------------------------------------------------
TOOL_QUOTA: Dict[str, Dict[str, int]] = {
    "browser_automation": {"max_concurrent_per_session": 1},
    "task_scheduler":     {"max_active_tasks_per_session": 20, "min_interval_seconds": 60},
    "doc_fetch":          {"max_concurrent_per_session": 3, "max_file_size_mb": 50},
    "fuzzy_edit":         {"max_replace_size_kb": 200},
}


# ---------------------------------------------------------------------------
# URL 黑名单：覆盖本地协议、内网网段、链路本地段
# ---------------------------------------------------------------------------
URL_BLOCKLIST_PATTERNS = [
    r"^file://",
    r"^chrome://",
    r"^javascript:",
    r"^data:",
    r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)",
    r"https?://10\.\d+\.\d+\.\d+",
    r"https?://172\.(1[6-9]|2\d|3[01])\.\d+\.\d+",
    r"https?://192\.168\.\d+\.\d+",
    r"https?://169\.254\.",  # link-local
    # IPv6 内网地址
    r"https?://\[::1\]",
    r"https?://\[fe80:",
    r"https?://\[fd[0-9a-fA-F]{2}:",
    # DNS rebinding 常见服务
    r"https?://([^/]*\.)?nip\.io",
    r"https?://([^/]*\.)?xip\.io",
    r"https?://([^/]*\.)?localtest\.me",
    r"https?://lvh\.me",
]


class Whitelist:
    """工具配额与 URL 黑名单的统一管理器（线程安全的内存计数器）。"""

    def __init__(self) -> None:
        # 预编译正则，提高 URL 校验性能
        self._url_patterns = [re.compile(p, re.IGNORECASE) for p in URL_BLOCKLIST_PATTERNS]

        # 并发计数器：{(tool_name, session_id): 当前并发数}
        self._concurrent_counters: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()

    # ---------------- 配额查询 ----------------

    def get_quota(self, tool_name: str) -> dict:
        """返回指定工具的配额配置；未配置工具返回空 dict。"""
        return dict(TOOL_QUOTA.get(tool_name, {}))

    # ---------------- URL 黑名单 ----------------

    def is_url_blocked(self, url: str) -> Tuple[bool, str]:
        """
        检测 URL 是否命中黑名单。

        Returns:
            (是否被拦截, 中文原因)
        """
        if not url or not isinstance(url, str):
            return True, "URL 为空或类型非法。"
        target = url.strip()
        if not target:
            return True, "URL 为空字符串。"

        for pattern in self._url_patterns:
            if pattern.search(target):
                return True, f"URL '{target}' 命中安全黑名单规则: {pattern.pattern}"
        return False, ""

    # ---------------- 并发控制 ----------------

    def check_concurrent(self, tool_name: str, session_id: str) -> Tuple[bool, str]:
        """
        校验当前 session 内的工具并发数是否超额。

        Returns:
            (是否允许, 拒绝原因/空串)
        """
        quota = TOOL_QUOTA.get(tool_name, {})
        max_concurrent = quota.get("max_concurrent_per_session")
        if max_concurrent is None:
            return True, ""

        key = (tool_name, session_id)
        with self._lock:
            current = self._concurrent_counters.get(key, 0)
            if current >= max_concurrent:
                return False, (f"工具 '{tool_name}' 在当前会话内的并发数已达上限 "
                               f"({current}/{max_concurrent})，请等待已有任务完成。")
        return True, ""

    def register_concurrent_start(self, tool_name: str, session_id: str) -> None:
        """登记一次并发占用。"""
        key = (tool_name, session_id)
        with self._lock:
            self._concurrent_counters[key] = self._concurrent_counters.get(key, 0) + 1

    def register_concurrent_end(self, tool_name: str, session_id: str) -> None:
        """释放一次并发占用；归零时清理键以避免内存泄漏。"""
        key = (tool_name, session_id)
        with self._lock:
            if key not in self._concurrent_counters:
                return
            self._concurrent_counters[key] -= 1
            if self._concurrent_counters[key] <= 0:
                del self._concurrent_counters[key]
