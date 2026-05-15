"""Registry module base classes for tools and skills."""

from abc import ABC, abstractmethod
from typing import Any, Dict


class BasePlugin(ABC):
    """Plugin base class defining the interface for all plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Plugin description."""
        pass

    @abstractmethod
    def execute(self, **kwargs: Any) -> Any:
        """
        Execute the plugin functionality.

        Args:
            **kwargs: Execution arguments

        Returns:
            Any: Execution result
        """
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """
        Get plugin schema for LLM to understand how to invoke this plugin.

        Returns:
            Dict[str, Any]: Schema dictionary
        """
        pass


class Tool(BasePlugin):
    """Tool base class for executable tools."""

    pass


class Skill(BasePlugin):
    """Skill base class for executable skills."""

    pass
