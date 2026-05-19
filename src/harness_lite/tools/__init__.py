"""工具模块"""

from harness_lite.registry.tool_registry import tool_registry
from harness_lite.tools.calculator import CalculatorTool
from harness_lite.tools.current_time import CurrentTimeTool
from harness_lite.tools.file_ops import (
    ListDirectoryTool,
    ReadFileTool,
    CreateFileTool,
    EditFileTool,
    GrepSearchTool
)
from harness_lite.tools.execution_ops import BashTerminalTool, PythonInterpreterTool
from harness_lite.tools.web_ops import IntelligenceSearchTool, WebScraperTool
from harness_lite.tools.skill_reader import ReadSkillTool

def register_all_tools():
    """自动注册所有内置工具"""
    tool_registry.register(CalculatorTool())
    tool_registry.register(CurrentTimeTool())
    tool_registry.register(ListDirectoryTool())
    tool_registry.register(ReadFileTool())
    tool_registry.register(CreateFileTool())
    tool_registry.register(EditFileTool())
    tool_registry.register(GrepSearchTool())
    tool_registry.register(PythonInterpreterTool())
    tool_registry.register(BashTerminalTool())
    tool_registry.register(WebScraperTool())
    tool_registry.register(IntelligenceSearchTool())
    tool_registry.register(ReadSkillTool())



# 自动执行注册
register_all_tools()

__all__ = [
    "CalculatorTool",
    "CurrentTimeTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "CreateFileTool",
    "EditFileTool",
    "GrepSearchTool",
    "WebScraperTool",
    "IntelligenceSearchTool",
    "BashTerminalTool",
    "PythonInterpreterTool",
    "ReadSkillTool"
]