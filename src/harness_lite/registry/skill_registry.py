"""Skill registry for managing skill plugins."""

from typing import Any, Dict, List, Optional

from .base import Skill


class SkillRegistry:
    """Registry for managing Skill instances."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """
        Register a skill.

        Args:
            skill: Skill instance to register

        Raises:
            ValueError: If skill with same name already exists
        """
        if skill.name in self._skills:
            raise ValueError(f"Skill with name '{skill.name}' already registered")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> bool:
        """
        Unregister a skill.

        Args:
            name: Name of the skill to unregister

        Returns:
            bool: True if skill was unregistered, False if not found
        """
        if name in self._skills:
            del self._skills[name]
            return True
        return False

    def get(self, name: str) -> Optional[Skill]:
        """
        Get a skill by name.

        Args:
            name: Name of the skill

        Returns:
            Optional[Skill]: Skill instance if found, None otherwise
        """
        return self._skills.get(name)

    def list_all(self) -> List[Dict[str, str]]:
        """
        List all registered skills.

        Returns:
            List[Dict[str, str]]: List of skill info dictionaries
        """
        return [
            {"name": skill.name, "description": skill.description}
            for skill in self._skills.values()
        ]

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """
        Get schemas for all registered skills.

        Returns:
            List[Dict[str, Any]]: List of skill schemas
        """
        return [skill.get_schema() for skill in self._skills.values()]


# Global singleton instance
skill_registry = SkillRegistry()
