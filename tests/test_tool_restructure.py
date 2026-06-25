"""阶段 D 测试 - 验证 tools 目录重构后注册表完整性。

覆盖点：
1. 16 个工具是否全部注册成功；
2. 每个工具 name 唯一、description 含中文字符；
3. 每个工具 get_schema() 是否符合 OpenAI function-calling 嵌套结构；
4. `current_session_id` / `process_manager` 是否仍能从 harness_lite.tools 顶层导入；
5. 全部工具实例化不抛异常。
"""
from __future__ import annotations

import re
import pytest

import harness_lite.tools  # noqa: F401  触发自动注册
from harness_lite.registry.tool_registry import tool_registry


EXPECTED_TOOL_NAMES = {
    "calculator",
    "current_time",
    "list_directory",
    "read_file",
    "create_file",
    "edit_file",
    "grep_search",
    "bash_terminal",
    "python_interpreter",
    "intelligence_search",
    "web_scraper",
    "read_skill",
    "fuzzy_edit",
    "doc_fetch",
    "task_scheduler",
    "browser_automation",
}

CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def _contains_chinese(text: str) -> bool:
    return bool(text) and bool(CHINESE_CHAR_RE.search(text))


def test_all_16_tools_registered():
    """16 个工具必须全部注册"""
    registered = {item["name"] for item in tool_registry.list_all()}
    missing = EXPECTED_TOOL_NAMES - registered
    extra = registered - EXPECTED_TOOL_NAMES
    assert not missing, f"缺少未注册的工具: {missing}"
    assert not extra, f"出现未预期的工具: {extra}"
    assert len(registered) == 16


def test_tool_descriptions_are_chinese():
    """每个工具的 description 必须包含中文（除 read_skill 等只读简介外，全部沿用中文规范）"""
    for item in tool_registry.list_all():
        tool = tool_registry.get(item["name"])
        assert tool is not None
        assert _contains_chinese(tool.description), (
            f"工具 {tool.name} 的 description 未包含中文: {tool.description!r}"
        )


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_NAMES))
def test_tool_schema_openai_function_calling_format(tool_name):
    """所有工具 schema 必须是嵌套的 OpenAI function-calling 格式"""
    tool = tool_registry.get(tool_name)
    schema = tool.get_schema()

    assert isinstance(schema, dict), f"{tool_name}: schema 不是 dict"
    assert schema.get("type") == "function", f"{tool_name}: 顶层 type 必须为 'function'"
    assert "function" in schema and isinstance(schema["function"], dict), (
        f"{tool_name}: 缺失嵌套 function 字段"
    )

    fn = schema["function"]
    assert fn.get("name") == tool_name, f"{tool_name}: function.name 不匹配"
    assert isinstance(fn.get("description"), str) and fn["description"], (
        f"{tool_name}: function.description 必须为非空字符串"
    )
    params = fn.get("parameters")
    assert isinstance(params, dict), f"{tool_name}: parameters 必须为 dict"
    assert params.get("type") == "object"
    assert "properties" in params and isinstance(params["properties"], dict)
    assert "required" in params and isinstance(params["required"], list)


def test_compat_imports_from_tools_top_level():
    """current_session_id 与 process_manager 必须能从 harness_lite.tools 顶层导入"""
    from harness_lite.tools import current_session_id, process_manager

    # current_session_id 是 ContextVar，必须有 get/set 方法
    assert hasattr(current_session_id, "get")
    assert hasattr(current_session_id, "set")
    # process_manager 必须暴露 Shell 进程获取接口
    assert process_manager is not None


def test_all_tools_instantiate_without_error():
    """所有工具类都应能再实例化一次（不依赖外部资源）"""
    from harness_lite.tools import (
        CalculatorTool, CurrentTimeTool, ListDirectoryTool, ReadFileTool,
        CreateFileTool, EditFileTool, GrepSearchTool, BashTerminalTool,
        PythonInterpreterTool, IntelligenceSearchTool, WebScraperTool,
        ReadSkillTool, FuzzyEditTool, DocFetchTool, TaskSchedulerTool,
        BrowserAutomationTool,
    )
    classes = [
        CalculatorTool, CurrentTimeTool, ListDirectoryTool, ReadFileTool,
        CreateFileTool, EditFileTool, GrepSearchTool, BashTerminalTool,
        PythonInterpreterTool, IntelligenceSearchTool, WebScraperTool,
        ReadSkillTool, FuzzyEditTool, DocFetchTool, TaskSchedulerTool,
        BrowserAutomationTool,
    ]
    assert len(classes) == 16
    for cls in classes:
        instance = cls()
        assert instance.name
        assert isinstance(instance.description, str)


def test_no_duplicate_tool_names():
    """注册表中不允许同名工具"""
    names = [item["name"] for item in tool_registry.list_all()]
    assert len(names) == len(set(names))
