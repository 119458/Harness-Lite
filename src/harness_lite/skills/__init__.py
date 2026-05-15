"""
技能模块

用于存放可扩展的技能实现。
每个技能独立文件夹，便于后续添加新技能。
"""

from harness_lite.registry.skill_registry import skill_registry
from harness_lite.skills.placeholder.skill import PlaceholderSkill


def register_all_skills():
    """自动注册所有内置技能"""
    skill_registry.register(PlaceholderSkill())


# 自动执行注册
register_all_skills()

__all__ = ["PlaceholderSkill"]