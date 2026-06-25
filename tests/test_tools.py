"""Tools module tests."""
import pytest
from harness_lite.tools.calculator import CalculatorTool
from harness_lite.tools.current_time import CurrentTimeTool
from harness_lite.tools.search import SearchTool


class TestCalculatorTool:
    """Calculator tool tests."""

    def test_addition(self):
        """Verify basic addition."""
        tool = CalculatorTool()
        assert tool.execute("2 + 3") == "5"

    def test_subtraction(self):
        """Verify subtraction."""
        tool = CalculatorTool()
        assert tool.execute("10 - 4") == "6"

    def test_multiplication(self):
        """Verify multiplication."""
        tool = CalculatorTool()
        assert tool.execute("2 * 3 * 4") == "24"

    def test_division(self):
        """Verify division."""
        tool = CalculatorTool()
        assert tool.execute("10 / 2") == "5.0"

    def test_complex_expression(self):
        """Verify complex expression with operator precedence."""
        tool = CalculatorTool()
        assert tool.execute("2 + 3 * 4") == "14"

    def test_with_parens(self):
        """Verify parentheses override precedence."""
        tool = CalculatorTool()
        assert tool.execute("(2 + 3) * 4") == "20"

    def test_power_operator(self):
        """Verify power operator."""
        tool = CalculatorTool()
        assert tool.execute("2 ** 3") == "8.0"

    def test_unary_negative(self):
        """Verify unary negative."""
        tool = CalculatorTool()
        assert tool.execute("-5 + 3") == "-2"

    def test_unary_positive(self):
        """Verify unary positive."""
        tool = CalculatorTool()
        assert tool.execute("+5") == "5"

    def test_get_schema(self):
        """Verify schema returns correct structure."""
        tool = CalculatorTool()
        schema = tool.get_schema()
        assert schema["name"] == "calculator"
        assert "parameters" in schema
        assert "expression" in schema["parameters"]["properties"]

    def test_invalid_expression_raises(self):
        """Verify invalid expression raises ValueError."""
        tool = CalculatorTool()
        with pytest.raises(ValueError):
            tool.execute("invalid + expression")


class TestCurrentTimeTool:
    """Current time tool tests."""

    def test_execute_returns_string(self):
        """Verify execute returns a string."""
        tool = CurrentTimeTool()
        result = tool.execute()
        assert isinstance(result, str)

    def test_default_format(self):
        """Verify default format returns datetime with time."""
        tool = CurrentTimeTool()
        result = tool.execute()
        # Default format is "%Y-%m-%d %H:%M:%S"
        assert len(result) == 19

    def test_custom_format(self):
        """Verify custom format works."""
        tool = CurrentTimeTool()
        result = tool.execute("%Y-%m-%d")
        assert len(result) == 10  # YYYY-MM-DD
        assert result[4] == "-"
        assert result[7] == "-"

    def test_custom_format_hour_minute(self):
        """Verify custom format with hour and minute."""
        tool = CurrentTimeTool()
        result = tool.execute("%H:%M")
        assert ":" in result

    def test_get_schema(self):
        """Verify schema returns correct structure."""
        tool = CurrentTimeTool()
        schema = tool.get_schema()
        assert schema["name"] == "current_time"
        assert "format" in schema["parameters"]["properties"]


class TestSearchTool:
    """Search tool tests."""

    def test_execute_returns_placeholder(self):
        """Verify search tool returns placeholder message."""
        tool = SearchTool()
        result = tool.execute("test query")
        assert "预留" in result or "暂不支持" in result

    def test_execute_with_num_results(self):
        """Verify search tool accepts num_results parameter."""
        tool = SearchTool()
        result = tool.execute("test query", num_results=10)
        assert "预留" in result or "暂不支持" in result

    def test_get_schema(self):
        """Verify schema returns correct structure."""
        tool = SearchTool()
        schema = tool.get_schema()
        assert schema["name"] == "search"
        assert "query" in schema["parameters"]["properties"]
        assert "num_results" in schema["parameters"]["properties"]
        assert schema["parameters"]["required"] == ["query"]

    def test_tool_properties(self):
        """Verify tool name and description."""
        tool = SearchTool()
        assert tool.name == "search"
        assert "搜索" in tool.description


class TestToolsAutoRegistration:
    """Verify tools are automatically registered."""

    def test_all_tools_importable(self):
        """Verify all tool classes are importable."""
        from harness_lite.tools import CalculatorTool, CurrentTimeTool, SearchTool
        assert CalculatorTool is not None
        assert CurrentTimeTool is not None
        assert SearchTool is not None

    def test_tools_have_required_properties(self):
        """Verify all tools have name and description properties."""
        tools = [CalculatorTool(), CurrentTimeTool(), SearchTool()]
        for tool in tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "description")
            assert hasattr(tool, "execute")
            assert hasattr(tool, "get_schema")
