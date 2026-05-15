"""Skills module tests."""
import pytest


class TestPlaceholderSkill:
    """Placeholder skill tests."""

    def test_execute_returns_string(self):
        """Verify execute returns a string."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill

        skill = PlaceholderSkill()
        result = skill.execute()
        assert isinstance(result, str)

    def test_execute_returns_placeholder_message(self):
        """Verify execute returns placeholder message."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill

        skill = PlaceholderSkill()
        result = skill.execute()
        assert "预留" in result or "预留" in result

    def test_get_schema(self):
        """Verify schema returns correct structure."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill

        skill = PlaceholderSkill()
        schema = skill.get_schema()
        assert schema["name"] == "placeholder"
        assert "description" in schema
        assert "parameters" in schema

    def test_tool_properties(self):
        """Verify skill name and description."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill

        skill = PlaceholderSkill()
        assert skill.name == "placeholder"
        assert skill.description == "预留技能接口示例"

    def test_execute_with_kwargs(self):
        """Verify execute accepts arbitrary kwargs."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill

        skill = PlaceholderSkill()
        result = skill.execute(param1="value1", param2="value2")
        assert isinstance(result, str)


class TestSkillsAutoRegistration:
    """Verify skills are automatically registered."""

    def test_all_skills_importable(self):
        """Verify all skill classes are importable."""
        from harness_lite.skills import PlaceholderSkill
        assert PlaceholderSkill is not None

    def test_global_skill_registry_has_skills(self):
        """Verify global skill registry contains registered skills."""
        from harness_lite.registry import skill_registry

        # After import, placeholder skill should be registered
        skills = skill_registry.list_all()
        assert len(skills) >= 1
        skill_names = [s["name"] for s in skills]
        assert "placeholder" in skill_names


class TestSkillRegistryIntegration:
    """Skill registry integration tests."""

    def test_skill_can_be_registered(self):
        """Verify skill can be registered to global registry."""
        from harness_lite.registry import SkillRegistry, skill_registry
        from harness_lite.registry.base import Skill

        class TestSkill(Skill):
            @property
            def name(self) -> str:
                return "test_skill"

            @property
            def description(self) -> str:
                return "A test skill"

            def execute(self, **kwargs):
                return "test"

            def get_schema(self):
                return {"name": self.name, "description": self.description}

        test_registry = SkillRegistry()
        skill = TestSkill()
        test_registry.register(skill)

        assert test_registry.get("test_skill") is skill
        assert test_registry.list_all()[0]["name"] == "test_skill"

    def test_skill_inheritance(self):
        """Verify skill inherits from correct base class."""
        from harness_lite.skills.placeholder.skill import PlaceholderSkill
        from harness_lite.registry.base import Skill

        skill = PlaceholderSkill()
        assert isinstance(skill, Skill)
