"""递归内容搜索工具"""

import os
from pathlib import Path
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class GrepSearchTool(BaseTool):
    """
    全局内容检索工具
    """
    @property
    def name(self) -> str:
        return "grep_search"
    @property
    def description(self) -> str:
        return "在指定目录下递归搜索包含特定关键字的文件及行号。用于快速定位函数定义或变量。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "query": {
                "type": "string",
                "description": "要搜索的关键字或字符串"
            },
            "path": {
                "type": "string",
                "description": "搜索的起始目录，默认为当前目录 '.'"
            }
        }
        schema["function"]["parameters"]["required"] = ["query"]
        return schema

    def execute(self, query: str, path: str = ".") -> str:
        base_path = Path(path)
        if not base_path.exists() or not base_path.is_dir():
            return f"Error: 目录 '{path}' 不存在。"

        ignore_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}
        results = []

        try:
            for root, dirs, files in os.walk(base_path):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for file in files:
                    if file.endswith(('.png', '.jpg', '.jpeg', '.pyc', '.pdf', '.zip', '.tar', '.gz')):
                        continue

                    file_path = Path(root) / file
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            for i, line in enumerate(f):
                                if query in line:
                                    results.append(f"{file_path}:{i+1}: {line.strip()}")
                                    if len(results) > 50:
                                        return "\n".join(results) + f"\n\n... (超过 50 条结果，已截断。请使用更具体的 query)"
                    except (UnicodeDecodeError, PermissionError):
                        continue

            if not results:
                return f"No results found for '{query}' in directory '{path}'"
            return "\n".join(results)
        except Exception as e:
            return f"Error during search: {str(e)}"
