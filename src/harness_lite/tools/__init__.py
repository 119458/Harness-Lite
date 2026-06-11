"""工具模块

每个工具独立成包：``harness_lite.tools.<tool_name>``。
顶层模块负责把所有工具实例注册进 ``tool_registry``。

为了兼容历史代码，仍然在此处重新导出关键组件：
- ``current_session_id`` / ``process_manager``（原 ``execution_ops``）
- 五个文件操作工具类（原 ``file_ops``）
"""

from harness_lite.registry.tool_registry import tool_registry

# 基类
from harness_lite.tools.base import BaseTool

# 原子工具（每个工具一个包）
from harness_lite.tools.calculator import CalculatorTool
from harness_lite.tools.current_time import CurrentTimeTool
from harness_lite.tools.list_directory import ListDirectoryTool
from harness_lite.tools.read_file import ReadFileTool
from harness_lite.tools.create_file import CreateFileTool
from harness_lite.tools.edit_file import EditFileTool
from harness_lite.tools.grep_search import GrepSearchTool
from harness_lite.tools.bash_terminal import (
    BashTerminalTool,
    current_session_id,
    process_manager,
)
from harness_lite.tools.python_interpreter import PythonInterpreterTool
from harness_lite.tools.intelligence_search import IntelligenceSearchTool
from harness_lite.tools.web_scraper import WebScraperTool
from harness_lite.tools.read_skill import ReadSkillTool
from harness_lite.tools.fuzzy_edit import FuzzyEditTool
from harness_lite.tools.doc_fetch import DocFetchTool
from harness_lite.tools.task_scheduler import TaskSchedulerTool
from harness_lite.tools.browser_automation import BrowserAutomationTool


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
    tool_registry.register(FuzzyEditTool())
    tool_registry.register(DocFetchTool())
    tool_registry.register(TaskSchedulerTool())
    tool_registry.register(BrowserAutomationTool())


# 自动执行注册
register_all_tools()

__all__ = [
    "BaseTool",
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
    "ReadSkillTool",
    "FuzzyEditTool",
    "DocFetchTool",
    "TaskSchedulerTool",
    "BrowserAutomationTool",
    # 兼容历史导入路径
    "current_session_id",
    "process_manager",
]
