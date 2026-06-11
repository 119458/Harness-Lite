"""安全文件创建工具（不覆盖已存在文件）"""

from pathlib import Path
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class CreateFileTool(BaseTool):
    """
    安全的文件创建工具
    """
    @property
    def name(self) -> str:
        return "create_file"
    @property
    def description(self) -> str:
        return "创建一个新文件并写入初始内容。如果文件已存在，将拒绝操作以防止覆盖已有代码。"

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "file_path": {
                "type": "string",
                "description": "要创建的文件路径"
            },
            "content": {
                "type": "string",
                "description": "新文件的初始内容"
            }
        }
        schema["function"]["parameters"]["required"] = ["file_path", "content"]
        return schema

    def execute(self, file_path: str, content: str = "") -> str:
        path = Path(file_path)
        if path.exists():
            return f"Error: 文件 '{file_path}' 已存在。请使用 edit_file 工具进行局部修改，或换一个文件名。"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Success: 文件 '{file_path}' 已成功创建并写入内容。"
        except Exception as e:
            return f"Error creating file: {str(e)}"
