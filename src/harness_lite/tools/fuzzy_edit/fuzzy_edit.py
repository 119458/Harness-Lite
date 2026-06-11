"""按文本片段模糊匹配后替换的文件编辑工具

定位：作为 `edit_file`（按行号区间替换）的补充。
当 LLM 知道"要改哪段内容"但不知道精确行号时，使用本工具更安全。

设计要点：
- old_text 为空串时退化为追加模式（写到文件末尾）。
- 命中多处时直接报错，避免误改。
- 自动处理 BOM 与 CRLF/LF/CR 换行差异。
- 单次替换上限 200 KB，防止内存爆炸。
- 不主动做路径沙箱校验（交给 security 拦截层），但在文件不存在 / 权限缺失时给出友好提示。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from harness_lite.tools.base import BaseTool
from harness_lite.tools.utils.diff_helper import (
    count_occurrences,
    fuzzy_find_in_content,
    make_unified_diff,
    normalize_line_endings,
    restore_line_endings,
    strip_bom,
)

_MAX_SNIPPET_BYTES = 200 * 1024  # 单次 old/new 文本上限 200 KB


class FuzzyEditTool(BaseTool):
    """通过文本片段精确/模糊匹配的方式编辑文件"""

    @property
    def name(self) -> str:
        return "fuzzy_edit"

    @property
    def description(self) -> str:
        return (
            "按文本片段定位并替换文件内容。优先精确匹配，匹配失败会尝试空白归一化的模糊匹配。"
            "old_text 留空时将 new_text 追加到文件末尾。命中多个位置会报错，避免误改。"
            "适合 LLM 仅知道改动片段但不知道精确行号的场景，是 edit_file 的补充。"
        )

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "目标文件路径，可使用相对或绝对路径，必须位于授权沙箱内。"
            },
            "old_text": {
                "type": "string",
                "description": "待替换的原文本片段。留空字符串表示追加模式，会把 new_text 追加到文件末尾。"
            },
            "new_text": {
                "type": "string",
                "description": "替换后的新文本，请保留正确的缩进与换行符。"
            },
        }
        schema["function"]["parameters"]["required"] = ["file_path", "old_text", "new_text"]
        return schema

    def execute(self, file_path: str, old_text: str, new_text: str) -> str:
        if not file_path or not file_path.strip():
            return "Error: file_path 不能为空。"

        size_err = self._check_payload_size(old_text, new_text)
        if size_err:
            return size_err

        absolute = self._resolve_path(file_path)
        existence_err = self._check_file_ready(absolute, file_path)
        if existence_err:
            return existence_err

        try:
            # 注意: newline="" 关闭 universal newlines, 保留原始 \r\n / \r / \n,
            # 否则 normalize_line_endings 永远拿不到 CRLF, 写回时会静默丢失回车。
            with absolute.open("r", encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except UnicodeDecodeError:
            return f"Error: 文件 '{file_path}' 不是合法的 UTF-8 文本，无法编辑。"
        except OSError as exc:
            return f"Error: 读取文件失败 ({exc})。"

        try:
            new_content, base_content = self._compute_new_content(raw, old_text, new_text, file_path)
        except _EditError as err:
            return str(err)

        bom_prefix, body_after = strip_bom(raw)
        _, original_ending = normalize_line_endings(body_after)
        try:
            final_text = bom_prefix + restore_line_endings(new_content, original_ending)
            # 注意: newline="" 防止 Python 在 Windows 上再做一次 \n -> \r\n 转换,
            # 与读取端对称, 完全由 restore_line_endings 决定最终行尾。
            with absolute.open("w", encoding="utf-8", newline="") as fh:
                fh.write(final_text)
        except OSError as exc:
            return f"Error: 写入文件失败 ({exc})。"

        diff_text = make_unified_diff(base_content, new_content, absolute.name)
        if not diff_text:
            diff_text = "(内容相同，未生成 diff)"
        return f"Success: 已更新 '{file_path}'。\n--- diff ---\n{diff_text}"

    # ---------- 内部辅助 ----------

    @staticmethod
    def _check_payload_size(old_text: str, new_text: str) -> str:
        for label, payload in (("old_text", old_text), ("new_text", new_text)):
            if len(payload.encode("utf-8", errors="ignore")) > _MAX_SNIPPET_BYTES:
                return f"Error: {label} 长度超过 200KB 上限，请缩小单次修改范围。"
        return ""

    @staticmethod
    def _resolve_path(file_path: str) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        return path

    @staticmethod
    def _check_file_ready(absolute: Path, original: str) -> str:
        if not absolute.exists():
            return f"Error: 文件 '{original}' 不存在，请先用 create_file 创建或确认路径。"
        if not absolute.is_file():
            return f"Error: 路径 '{original}' 不是普通文件。"
        if not os.access(absolute, os.R_OK | os.W_OK):
            return f"Error: 文件 '{original}' 缺少读写权限，可能越出沙箱授权范围。"
        return ""

    def _compute_new_content(self, raw: str, old_text: str,
                             new_text: str, file_path: str) -> tuple[str, str]:
        """计算替换后的文本，返回 (new_content, base_content)（都已归一化为 LF）"""
        _, body_after_bom = strip_bom(raw)
        normalized_body, _ = normalize_line_endings(body_after_bom)
        normalized_new, _ = normalize_line_endings(new_text)

        # 空字符串/纯空白视为追加
        if not old_text or not old_text.strip():
            if normalized_body and not normalized_body.endswith("\n"):
                new_content = normalized_body + "\n" + normalized_new
            else:
                new_content = normalized_body + normalized_new
            if new_content == normalized_body:
                raise _EditError(f"Error: '{file_path}' 追加的新内容为空，未做修改。")
            return new_content, normalized_body

        normalized_old, _ = normalize_line_endings(old_text)
        match = fuzzy_find_in_content(normalized_body, normalized_old)
        if not match.found:
            raise _EditError(
                f"Error: 在 '{file_path}' 中未找到匹配的 old_text 片段。"
                "请用 read_file 复核需要修改的文本，确保包含足够上下文且空白结构一致。"
            )

        occurrences = count_occurrences(normalized_body, normalized_old)
        if occurrences > 1:
            raise _EditError(
                f"Error: old_text 在 '{file_path}' 中出现 {occurrences} 次，无法唯一定位。"
                "请加入更多上下文使其唯一。"
            )

        base = match.working_content
        new_content = (
            base[: match.start_idx]
            + normalized_new
            + base[match.start_idx + match.matched_length:]
        )
        if new_content == base:
            raise _EditError(f"Error: 替换前后内容完全相同，未对 '{file_path}' 做任何修改。")
        return new_content, base


class _EditError(Exception):
    """内部错误信号，message 会原样回传给 LLM"""
