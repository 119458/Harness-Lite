"""SKILL.md 正文读取工具"""

from typing import Dict, Any

from harness_lite.tools.base import BaseTool
from harness_lite.registry.skill_registry import skill_registry


class ReadSkillTool(BaseTool):
    """
    供大模型按需翻阅 SKILL.md 正文的专属工具
    """
    @property
    def name(self) -> str:
        return "read_skill"
    @property
    def description(self) -> str:
        return "查阅特定业务技能 (Skill) 或标准操作规范 (SOP) 的完整详细内容。在执行相关领域的专业任务前，请先调用此工具获取背景规范指导。"

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "skill_name": {
                "type": "string",
                "description": "需要查阅的技能名称（请严格使用系统提示词【可用业务技能】列表中提供的名称）"
            }
        }
        schema["function"]["parameters"]["required"] = ["skill_name"]
        return schema

    def execute(self, skill_name: str) -> str:
        skill = skill_registry.get_skill(skill_name)

        if not skill:
            return f"Error: 找不到名为 '{skill_name}' 的技能。请确认拼写是否与系统列表完全一致。"

        content = getattr(skill, "content", None)
        if not content:
            return f"Error: 技能 '{skill_name}' 存在，但内部没有任何可供读取的文本内容。"
        return f"=== 【{skill_name}】的执行规范与 SOP 正文 ===\n\n{content}\n\n====================\n提示：请大模型严格按照上述规范要求，配合现有的物理工具开始执行任务。"
