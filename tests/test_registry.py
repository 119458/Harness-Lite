"""Registry module tests."""
import pytest
from harness_lite.registry.base import Tool, Skill


class MockTool(Tool):
    """Mock tool for testing."""

    @property
    def name(self) -> str:
        return "mock_tool"

    @property
    def description(self) -> str:
        return "A mock tool"

    def execute(self, **kwargs):
        return "executed"

    def get_schema(self):
        return {"name": self.name, "description": self.description}


class MockSkill(Skill):
    """Mock skill for testing."""

    @property
    def name(self) -> str:
        return "mock_skill"

    @property
    def description(self) -> str:
        return "A mock skill"

    def execute(self, **kwargs):
        return "skill_executed"

    def get_schema(self):
        return {"name": self.name, "description": self.description}


class TestToolRegistry:
    """Tool registry tests."""

    def test_register_and_get(self):
        """Verify tool can be registered and retrieved."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)
        assert registry.get("mock_tool") is tool

    def test_register_duplicate_raises(self):
        """Verify registering duplicate tool raises ValueError."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(tool)

    def test_unregister(self):
        """Verify tool can be unregistered."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)
        assert registry.unregister("mock_tool") is True
        assert registry.get("mock_tool") is None

    def test_unregister_nonexistent_returns_false(self):
        """Verify unregistering nonexistent tool returns False."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.unregister("nonexistent") is False

    def test_list_all(self):
        """Verify listing all tools returns tool info."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(MockTool())
        tools = registry.list_all()
        assert len(tools) == 1
        assert tools[0]["name"] == "mock_tool"

    def test_get_all_schemas(self):
        """Verify getting all schemas returns tool schemas."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(MockTool())
        schemas = registry.get_all_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "mock_tool"

    def test_get_nonexistent_returns_none(self):
        """Verify getting nonexistent tool returns None."""
        from harness_lite.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.get("nonexistent") is None


class TestSkillRegistry:
    """Skill registry tests."""

    def test_register_and_get(self):
        """Verify skill can be registered and retrieved."""
        from harness_lite.registry import SkillRegistry

        registry = SkillRegistry()
        skill = MockSkill()
        registry.register(skill)
        assert registry.get("mock_skill") is skill

    def test_register_duplicate_raises(self):
        """Verify registering duplicate skill raises ValueError."""
        from harness_lite.registry import SkillRegistry

        registry = SkillRegistry()
        skill = MockSkill()
        registry.register(skill)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(skill)

    def test_unregister(self):
        """Verify skill can be unregistered."""
        from harness_lite.registry import SkillRegistry

        registry = SkillRegistry()
        skill = MockSkill()
        registry.register(skill)
        assert registry.unregister("mock_skill") is True
        assert registry.get("mock_skill") is None

    def test_list_all(self):
        """Verify listing all skills returns skill info."""
        from harness_lite.registry import SkillRegistry

        registry = SkillRegistry()
        registry.register(MockSkill())
        skills = registry.list_all()
        assert len(skills) == 1
        assert skills[0]["name"] == "mock_skill"

    def test_get_all_schemas(self):
        """Verify getting all schemas returns skill schemas."""
        from harness_lite.registry import SkillRegistry

        registry = SkillRegistry()
        registry.register(MockSkill())
        schemas = registry.get_all_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "mock_skill"


class TestToolAndSkillDecoupling:
    """Verify module decoupling - tools and skills are independent."""

    def test_tool_registry_independent_of_skill_registry(self):
        """Verify ToolRegistry and SkillRegistry are separate instances."""
        from harness_lite.registry import ToolRegistry, SkillRegistry

        tool_registry = ToolRegistry()
        skill_registry = SkillRegistry()

        tool = MockTool()
        skill = MockSkill()

        tool_registry.register(tool)
        skill_registry.register(skill)

        # Verify they don't interfere
        assert tool_registry.get("mock_tool") is tool
        assert skill_registry.get("mock_skill") is skill
        assert tool_registry.get("mock_skill") is None
        assert skill_registry.get("mock_tool") is None

    def test_global_registries_exist(self):
        """Verify global singleton registries exist."""
        from harness_lite.registry import tool_registry, skill_registry

        assert tool_registry is not None
        assert skill_registry is not None
