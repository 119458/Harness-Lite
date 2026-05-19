import os
import pytest
from pathlib import Path

# 假设你的工具类保存在 src/harness_lite/tools/file_ops.py 中
from harness_lite.tools.file_ops import (
    ListDirectoryTool,
    ReadFileTool,
    CreateFileTool,
    EditFileTool,
    GrepSearchTool
)


@pytest.fixture
def setup_tools():
    """初始化工具实例的 Fixture"""
    return {
        "list_dir": ListDirectoryTool(),
        "read_file": ReadFileTool(),
        "create_file": CreateFileTool(),
        "edit_file": EditFileTool(),
        "grep": GrepSearchTool()
    }


def test_create_file_tool(setup_tools, tmp_path):
    """测试创建文件工具"""
    tool = setup_tools["create_file"]
    file_path = str(tmp_path / "test_app.py")
    content = "def hello():\n    print('hello world')\n"

    # 1. 测试正常创建
    result = tool.execute(file_path=file_path, content=content)
    assert "Success" in result
    assert Path(file_path).read_text() == content

    # 2. 测试防止覆盖机制 (文件已存在)
    result_overwrite = tool.execute(file_path=file_path, content="def hack(): pass")
    assert "Error" in result_overwrite
    assert "已存在" in result_overwrite
    # 确保内容没有被修改
    assert Path(file_path).read_text() == content


def test_read_file_tool(setup_tools, tmp_path):
    """测试读取文件工具"""
    tool = setup_tools["read_file"]
    file_path = tmp_path / "data.txt"
    lines = ["Line 1\n", "Line 2\n", "Line 3\n", "Line 4\n"]
    file_path.write_text("".join(lines))

    # 1. 测试全量读取
    result_full = tool.execute(file_path=str(file_path))
    assert "   1 | Line 1" in result_full
    assert "   4 | Line 4" in result_full

    # 2. 测试局部读取 (按行号)
    result_partial = tool.execute(file_path=str(file_path), start_line=2, end_line=3)
    assert "   2 | Line 2" in result_partial
    assert "   3 | Line 3" in result_partial
    assert "Line 1" not in result_partial
    assert "Line 4" not in result_partial


def test_edit_file_tool(setup_tools, tmp_path):
    """测试精细化文件修改工具"""
    tool = setup_tools["edit_file"]
    file_path = tmp_path / "script.py"
    initial_content = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
    file_path.write_text(initial_content)

    # 1. 测试成功的精确替换
    old_str = "    return a + b\n"
    new_str = "    print('adding')\n    return a + b\n"
    result = tool.execute(file_path=str(file_path), old_str=old_str, new_str=new_str)

    assert "Success" in result
    updated_content = file_path.read_text()
    assert "print('adding')" in updated_content

    # 2. 测试找不到匹配字符串的情况 (防止随意修改)
    result_not_found = tool.execute(file_path=str(file_path), old_str="return a + c", new_str="pass")
    assert "Error" in result_not_found
    assert "未能" in result_not_found

    # 3. 测试存在多个匹配项的情况 (防止误伤)
    file_path.write_text("print('x')\nprint('x')\n")
    result_multiple = tool.execute(file_path=str(file_path), old_str="print('x')\n", new_str="print('y')\n")
    assert "Error" in result_multiple
    assert "找到了 2 处匹配" in result_multiple


def test_list_directory_tool(setup_tools, tmp_path):
    """测试目录树浏览工具"""
    tool = setup_tools["list_dir"]

    # 构建测试目录结构
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('main')")
    (tmp_path / ".git").mkdir()  # 应该被忽略
    (tmp_path / ".git" / "config").write_text("core")

    result = tool.execute(path=str(tmp_path), max_depth=2)

    # 验证正常文件和目录存在
    assert "📂 src/" in result
    assert "📄 main.py" in result

    # 验证忽略目录生效
    assert ".git" not in result
    assert "config" not in result


def test_grep_search_tool(setup_tools, tmp_path):
    """测试全局搜索工具"""
    tool = setup_tools["grep"]

    # 构建测试文件
    dir1 = tmp_path / "module_a"
    dir1.mkdir()
    (dir1 / "auth.py").write_text("def login():\n    SECRET_KEY = '123'\n    pass\n")

    dir2 = tmp_path / "module_b"
    dir2.mkdir()
    (dir2 / "api.py").write_text("from module_a.auth import SECRET_KEY\n")

    # 执行搜索
    result = tool.execute(query="SECRET_KEY", path=str(tmp_path))

    # 验证结果包含文件路径、行号和内容
    assert "auth.py:2: SECRET_KEY = '123'" in result
    assert "api.py:1: from module_a.auth import SECRET_KEY" in result

    # 测试搜索不存在的字符串
    result_empty = tool.execute(query="NOT_EXIST", path=str(tmp_path))
    assert "No results found" in result_empty