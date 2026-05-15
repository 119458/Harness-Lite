"""工具基类"""

from harness_lite.registry.base import Tool
from typing import Dict, Any


class BaseTool(Tool):
    """工具基类，继承自 registry.base.Tool"""

    def __init__(self):
        super().__init__()

    def get_schema(self) -> Dict[str, Any]:
        """
        返回工具的 schema 定义（OpenAI function calling 格式）

        Returns:
            OpenAI 兼容的 function calling schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }