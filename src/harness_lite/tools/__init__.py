"""工具模块"""

from harness_lite.registry.tool_registry import tool_registry
from harness_lite.tools.calculator import CalculatorTool
from harness_lite.tools.current_time import CurrentTimeTool
from harness_lite.tools.search import SearchTool


def register_all_tools():
    """自动注册所有内置工具"""
    tool_registry.register(CalculatorTool())
    tool_registry.register(CurrentTimeTool())
    tool_registry.register(SearchTool())


# 自动执行注册
register_all_tools()

__all__ = [
    "CalculatorTool",
    "CurrentTimeTool",
    "SearchTool",
]