"""Tool registry for managing tool plugins."""

from typing import Any, Dict, List, Optional

from .base import Tool


class ToolRegistry:
    """Registry for managing Tool instances."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """
        Register a tool.

        Args:
            tool: Tool instance to register

        Raises:
            ValueError: If tool with same name already exists
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool with name '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """
        Unregister a tool.

        Args:
            name: Name of the tool to unregister

        Returns:
            bool: True if tool was unregistered, False if not found
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> Optional[Tool]:
        """
        Get a tool by name.

        Args:
            name: Name of the tool

        Returns:
            Optional[Tool]: Tool instance if found, None otherwise
        """
        return self._tools.get(name)

    def list_all(self) -> List[Dict[str, str]]:
        """
        List all registered tools.

        Returns:
            List[Dict[str, str]]: List of tool info dictionaries
        """
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self._tools.values()
        ]

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """
        Get schemas for all registered tools.

        Returns:
            List[Dict[str, Any]]: List of tool schemas
        """
        return [tool.get_schema() for tool in self._tools.values()]


# Global singleton instance
tool_registry = ToolRegistry()
