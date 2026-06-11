"""文本模糊匹配与 unified diff 辅助函数

设计要点：
- 所有函数都是纯函数，不产生副作用，便于单元测试。
- 提供 BOM/换行符识别与还原，保证编辑后文件的字节级一致性。
- 模糊匹配只做"空白归一化"层级，不做语义匹配，避免改错。
"""

from __future__ import annotations

import difflib
import re
from typing import NamedTuple, Tuple

_BOM_CHAR = "\ufeff"
_WS_RUN_PATTERN = re.compile(r"[ \t]+")
_TAILING_WS_BEFORE_LF = re.compile(r"[ \t]+\n")


class FuzzyMatch(NamedTuple):
    """模糊匹配结果

    属性说明：
    - found: 是否找到
    - start_idx: 命中位置（基于 working_content 的字符下标）
    - matched_length: 命中片段长度
    - working_content: 实际用于替换的文本（精确命中为原文，模糊命中为归一化后文本）
    """
    found: bool
    start_idx: int
    matched_length: int
    working_content: str


def strip_bom(text: str) -> Tuple[str, str]:
    """剥离文本开头的 BOM 字节

    Args:
        text: 原始文本

    Returns:
        (bom_prefix, body): bom_prefix 要么是空串，要么是单个 \ufeff
    """
    if text.startswith(_BOM_CHAR):
        return _BOM_CHAR, text[1:]
    return "", text


def normalize_line_endings(text: str) -> Tuple[str, str]:
    """将换行符统一为 LF，并记录原本的换行格式

    Args:
        text: 原始文本

    Returns:
        (normalized_text, detected_ending)
        detected_ending 取值 "\r\n"、"\r" 或 "\n"
    """
    if "\r\n" in text:
        detected = "\r\n"
    elif "\r" in text:
        detected = "\r"
    else:
        detected = "\n"
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return body, detected


def restore_line_endings(text: str, original_ending: str) -> str:
    """将归一化为 LF 的文本还原回原始换行风格"""
    if original_ending == "\r\n":
        return text.replace("\n", "\r\n")
    if original_ending == "\r":
        return text.replace("\n", "\r")
    return text


def _whitespace_normalize(text: str) -> str:
    """空白归一化：压缩水平空白、剔除行尾尾随空白，但保留缩进位置

    用于模糊匹配阶段，让 LLM 提供的片段在轻微缩进差异下仍能命中。
    """
    text = _WS_RUN_PATTERN.sub(" ", text)
    text = _TAILING_WS_BEFORE_LF.sub("\n", text)
    return text


def fuzzy_find_in_content(content: str, snippet: str) -> FuzzyMatch:
    """先精确匹配，失败再做空白归一化匹配

    Args:
        content: 完整文件内容
        snippet: 待查找的文本片段

    Returns:
        FuzzyMatch；未命中时 found=False
    """
    if not snippet:
        return FuzzyMatch(False, -1, 0, content)

    direct_idx = content.find(snippet)
    if direct_idx != -1:
        return FuzzyMatch(True, direct_idx, len(snippet), content)

    normalized_content = _whitespace_normalize(content)
    normalized_snippet = _whitespace_normalize(snippet)
    fuzzy_idx = normalized_content.find(normalized_snippet)
    if fuzzy_idx != -1:
        return FuzzyMatch(True, fuzzy_idx, len(normalized_snippet), normalized_content)

    return FuzzyMatch(False, -1, 0, content)


def count_occurrences(content: str, snippet: str) -> int:
    """统计 snippet 在 content 中的出现次数（先用归一化，再用精确）"""
    if not snippet:
        return 0
    exact = content.count(snippet)
    if exact > 0:
        return exact
    return _whitespace_normalize(content).count(_whitespace_normalize(snippet))


def make_unified_diff(old: str, new: str, filename: str) -> str:
    """生成 unified diff 字符串

    Args:
        old: 修改前文本
        new: 修改后文本
        filename: 用于 diff 头部展示的逻辑名

    Returns:
        diff 字符串；若内容完全一致返回空串
    """
    if old == new:
        return ""
    diff_lines = difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    return "\n".join(diff_lines)
