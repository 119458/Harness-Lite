"""基于行号区间的文件替换工具"""

import os
from pathlib import Path
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class EditFileTool(BaseTool):
    """
    基于行号区间的精确文件修改工具 (Line-based Replacer)
    """
    @property
    def name(self) -> str:
        return "edit_file"
    @property
    def description(self) -> str:
        return "修改文件的指定行号区间。你需要提供起始行、结束行以及替换的新内容。建议修改前先用 read_file 工具确认准确的行号。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要修改的文件路径 (相对沙箱根目录)"
            },
            "start_line": {
                "type": "integer",
                "description": "要替换的起始行号（包含，从 1 开始）。如果要插入到文件头部，请设为 1。"
            },
            "end_line": {
                "type": "integer",
                "description": "要替换的结束行号（包含）。如果要删除这些行，将 new_content 留空即可。"
            },
            "new_content": {
                "type": "string",
                "description": "替换后的新代码内容。请确保包含正确的缩进和换行符。"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path", "start_line", "end_line", "new_content"]
        return schema

    def execute(self, file_path: str, start_line: int, end_line: int, new_content: str) -> str:
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(os.getcwd()) / path

        if not path.exists() or not path.is_file():
            return f"Error: 文件 '{file_path}' 不存在。请确认路径是否正确。"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) 不能大于 end_line ({end_line})。"

        try:
            # newline="" 保留原始行尾（CRLF/LF），避免静默把 \r\n 改为 \n
            with open(path, "r", encoding="utf-8", newline="") as f:
                lines = f.readlines()
            total_lines = len(lines)

            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, end_line)

            if new_content and not new_content.endswith("\n"):
                new_content += "\n"

            new_lines = lines[: start_idx] + ([new_content] if new_content else []) + lines[end_idx:]

            with open(path, "w", encoding="utf-8", newline="") as f:
                f.writelines(new_lines)
            return f"Success: 已将 '{file_path}' 的第 {start_line} 到 {end_line} 行替换为新内容。当前文件总行数变为 {len(new_lines)} 行。"
        except Exception as e:
            return f"Error editing file: {str(e)}"
