"""Registry module for tools and skills.

This module provides registration mechanisms for tools and skills,
enabling a plug-and-play architecture.
"""

from .base import BasePlugin, Skill, Tool
from .skill_registry import SkillRegistry, skill_registry
from .tool_registry import ToolRegistry, tool_registry

__all__ = [
    "BasePlugin",
    "Tool",
    "Skill",
    "ToolRegistry",
    "tool_registry",
    "SkillRegistry",
    "skill_registry",
]
