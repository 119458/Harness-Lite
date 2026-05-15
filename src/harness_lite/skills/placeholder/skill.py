"""预留技能示例"""

from typing import Dict, Any

from harness_lite.registry.base import Skill


class PlaceholderSkill(Skill):
    """预留技能示例"""

    @property
    def name(self) -> str:
        return "placeholder"

    @property
    def description(self) -> str:
        return "预留技能接口示例"

    def execute(self, **kwargs) -> Any:
        """
        执行技能

        Args:
            **kwargs: 技能参数

        Returns:
            技能执行结果
        """
        return "这是一个预留技能接口"

    def get_schema(self) -> Dict[str, Any]:
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