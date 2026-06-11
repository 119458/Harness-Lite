"""工具共享辅助模块

提供模糊文本匹配、unified diff 生成、输出截断等通用函数，
供 fuzzy_edit、doc_fetch、browser_automation 等工具复用。
"""

from .diff_helper import (
    strip_bom,
    normalize_line_endings,
    fuzzy_find_in_content,
    make_unified_diff,
)
from .output_truncate import (
    truncate_from_head,
    truncate_from_tail,
    human_readable_size,
)

__all__ = [
    "strip_bom",
    "normalize_line_endings",
    "fuzzy_find_in_content",
    "make_unified_diff",
    "truncate_from_head",
    "truncate_from_tail",
    "human_readable_size",
]
