from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from harness_lite.skills.types import Skill

class SkillRegistry:
    """
    管理所有纯文本 SOP 技能的内存注册表
    """

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get_skill(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        return list(self._skills.values())

skill_registry = SkillRegistry()