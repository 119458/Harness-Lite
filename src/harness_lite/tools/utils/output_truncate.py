"""工具输出截断辅助

针对超长工具输出（文件读取、文档解析、Shell stdout 等）做"行 + 字节"
双维度截断：任一维度命中阈值就触发截断。

- truncate_from_head: 保留开头 N 行/字节，适合文档阅读类工具
- truncate_from_tail: 保留末尾 N 行/字节，适合命令执行类工具（错误通常在末尾）
"""

from __future__ import annotations

from typing import Tuple

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50 KB


def human_readable_size(num_bytes: int) -> str:
    """把字节数转成 B/KB/MB 可读字符串"""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def _split_text(text: str) -> Tuple[list, int]:
    """切分文本为行，并返回总字节数"""
    return text.split("\n"), len(text.encode("utf-8"))


def _is_within_limits(total_lines: int, total_bytes: int,
                      max_lines: int, max_bytes: int) -> bool:
    return total_lines <= max_lines and total_bytes <= max_bytes


def truncate_from_head(text: str,
                       max_lines: int = DEFAULT_MAX_LINES,
                       max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[str, bool, str]:
    """保留开头若干行/字节

    Args:
        text: 原始文本
        max_lines: 最大行数
        max_bytes: 最大字节数（按 UTF-8 编码计算）

    Returns:
        (truncated_text, was_truncated, reason)
        reason 取值："none"、"lines"、"bytes"、"first_line_too_long"
    """
    if not text:
        return text, False, "none"

    lines, total_bytes = _split_text(text)
    total_lines = len(lines)

    if _is_within_limits(total_lines, total_bytes, max_lines, max_bytes):
        return text, False, "none"

    first_line_bytes = len(lines[0].encode("utf-8"))
    if first_line_bytes > max_bytes:
        return "", True, "first_line_too_long"

    kept: list = []
    used_bytes = 0
    reason = "lines"
    for idx, line in enumerate(lines):
        if idx >= max_lines:
            break
        # 第二行起需要包含前置的换行符
        line_cost = len(line.encode("utf-8")) + (1 if idx > 0 else 0)
        if used_bytes + line_cost > max_bytes:
            reason = "bytes"
            break
        kept.append(line)
        used_bytes += line_cost

    if len(kept) >= max_lines and used_bytes <= max_bytes:
        reason = "lines"

    return "\n".join(kept), True, reason


def truncate_from_tail(text: str,
                       max_lines: int = DEFAULT_MAX_LINES,
                       max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[str, bool, str]:
    """保留末尾若干行/字节

    Args:
        text: 原始文本
        max_lines: 最大行数
        max_bytes: 最大字节数（按 UTF-8 编码计算）

    Returns:
        (truncated_text, was_truncated, reason)
    """
    if not text:
        return text, False, "none"

    lines, total_bytes = _split_text(text)
    total_lines = len(lines)

    if _is_within_limits(total_lines, total_bytes, max_lines, max_bytes):
        return text, False, "none"

    kept: list = []
    used_bytes = 0
    reason = "lines"
    for idx in range(total_lines - 1, -1, -1):
        if len(kept) >= max_lines:
            break
        line = lines[idx]
        line_cost = len(line.encode("utf-8")) + (1 if kept else 0)
        if used_bytes + line_cost > max_bytes:
            reason = "bytes"
            break
        kept.insert(0, line)
        used_bytes += line_cost

    if len(kept) >= max_lines and used_bytes <= max_bytes:
        reason = "lines"

    return "\n".join(kept), True, reason
