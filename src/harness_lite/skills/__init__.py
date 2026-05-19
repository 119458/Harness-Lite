"""
技能模块

用于存放可扩展的技能实现。
每个技能独立文件夹，便于后续添加新技能。
"""
import os
from harness_lite.skills.loader import load_skills_from_directory
from harness_lite.registry.skill_registry import skill_registry

_SKILLS_DIR = os.path.dirname(os.path.abspath(__file__))

def _auto_register_skills():
    """
    模块初始化时的内部函数：自动扫描当前目录并注册所有合法的 SKILL.md
    """
    loaded_skills = load_skills_from_directory(_SKILLS_DIR)
    registered_count = 0
    for skill in loaded_skills:
        skill_registry.register(skill)
        registered_count += 1

_auto_register_skills()
__all__ = []