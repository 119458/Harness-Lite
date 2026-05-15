"""当前时间工具"""

from datetime import datetime
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class CurrentTimeTool(BaseTool):
    """获取当前时间"""

    @property
    def name(self) -> str:
        return "current_time"

    @property
    def description(self) -> str:
        return "获取当前的日期和时间"

    def execute(self, format: str = "%Y-%m-%d %H:%M:%S") -> str:
        """
        获取当前时间

        Args:
            format: 时间格式，默认 "%Y-%m-%d %H:%M:%S"

        Returns:
            格式化后的时间字符串
        """
        now = datetime.now()
        return now.strftime(format)

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "description": "时间格式，默认 \"%Y-%m-%d %H:%M:%S\""
                        }
                    },
                    "required": []
                }
            }
        }