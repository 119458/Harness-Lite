"""搜索工具（预留接口）"""

from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class SearchTool(BaseTool):
    """网络搜索工具（预留接口）"""

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "执行网络搜索，查找相关信息"

    def execute(self, query: str, num_results: int = 5) -> str:
        """
        执行搜索

        Args:
            query: 搜索关键词
            num_results: 返回结果数量，默认 5

        Returns:
            搜索结果描述（预留接口，当前返回提示）
        """
        return "搜索功能预留接口，当前版本暂不支持"

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词"
                        },
                        "num_results": {
                            "type": "integer",
                            "description": "返回结果数量，默认 5"
                        }
                    },
                    "required": ["query"]
                }
            }
        }