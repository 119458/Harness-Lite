"""按行号范围读取文件工具"""

from pathlib import Path
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class ReadFileTool(BaseTool):
    """
    带有行号限制的文件读取工具，防止一次读取过大文件
    """
    @property
    def name(self) -> str:
        return "read_file"
    @property
    def description(self) -> str:
        return "读取本地文件的内容。强烈建议指定行号范围，以便在审查大文件时节省上下文。"
    def __init__(self):
        super().__init__()


    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要读取的文件绝对或相对路径"
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（从 1 开始），可选。默认 1。"
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号，可选。默认读取到文件末尾。"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path"]
        return schema

    def execute(self, file_path: str, start_line: int = 1, end_line: int = -1) -> str:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return f"Error: 文件 '{file_path}' 不存在。"

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = total_lines if end_line == -1 else min(total_lines, end_line)

            if start_idx >= total_lines:
                return f"Error: start_line ({start_line}) 超出了文件总行数 ({total_lines})。"

            content_lines = []
            for i in range(start_idx, end_idx):
                content_lines.append(f"{i + 1:4d} | {lines[i]}")

            return f"--- File: {file_path} (Lines: {start_idx + 1} to {end_idx}, Total: {total_lines}) ---\n" + "".join(content_lines)
        except UnicodeDecodeError:
            return f"Error: 文件 '{file_path}' 似乎是二进制文件，无法作为文本读取。"
        except Exception as e:
            return f"Error reading file: {str(e)}"
