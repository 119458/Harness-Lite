"""阶段 D 测试 - fuzzy_edit 工具

覆盖：精确匹配 / 模糊匹配 / 多匹配报错 / 不匹配报错 / 追加模式 /
体积超限被工具或安全层拦截 / 路径越界被安全层拦截 / diff 输出。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import harness_lite.tools  # noqa: F401  确保已注册
from harness_lite.tools import FuzzyEditTool
from harness_lite.security.manager import SecurityManager


@pytest.fixture
def tool():
    return FuzzyEditTool()


@pytest.fixture
def isolated_security(tmp_path, monkeypatch):
    """创建一个把沙箱根指向 tmp_path 的 SecurityManager，避免污染真实 sandbox/"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return SecurityManager()


def test_exact_match_replace(tool, tmp_path):
    """精确匹配 -> 替换成功 + Success 提示 + 含 diff"""
    file_path = tmp_path / "demo.py"
    file_path.write_text("def add(a, b):\n    return a + b\n")

    result = tool.execute(
        file_path=str(file_path),
        old_text="return a + b",
        new_text="return a + b + 1",
    )

    assert result.startswith("Success"), result
    assert "--- diff ---" in result
    assert "+    return a + b + 1" in result or "return a + b + 1" in result
    assert file_path.read_text() == "def add(a, b):\n    return a + b + 1\n"


def test_fuzzy_match_extra_whitespace(tool, tmp_path):
    """文件含多余空格时仍可命中，证明空白归一化模糊匹配"""
    file_path = tmp_path / "spaced.py"
    # 真实文件多了空格
    file_path.write_text("x  =   1\n")

    # LLM 传入只有单空格的版本
    result = tool.execute(
        file_path=str(file_path),
        old_text="x = 1",
        new_text="x = 99",
    )
    assert result.startswith("Success"), result
    # 替换之后内容包含新值
    assert "x = 99" in file_path.read_text()


def test_fuzzy_match_crlf_vs_lf(tool, tmp_path):
    """文件为 CRLF、LLM 传 LF 也能成功匹配并替换内容。"""
    file_path = tmp_path / "crlf.txt"
    file_path.write_bytes(b"hello\r\nworld\r\n")

    result = tool.execute(
        file_path=str(file_path),
        old_text="world\n",
        new_text="planet\n",
    )
    assert result.startswith("Success"), result
    final_bytes = file_path.read_bytes()
    # 至少匹配并替换成功
    assert b"planet" in final_bytes
    assert b"world" not in final_bytes


def test_fuzzy_edit_should_preserve_crlf(tool, tmp_path):
    """期望但当前未实现：CRLF 文件在编辑后应保留 CRLF。"""
    file_path = tmp_path / "crlf2.txt"
    file_path.write_bytes(b"hello\r\nworld\r\n")
    tool.execute(file_path=str(file_path), old_text="world\n", new_text="planet\n")
    assert b"\r\n" in file_path.read_bytes()


def test_multiple_matches_error(tool, tmp_path):
    """命中多处直接报错，避免误改"""
    file_path = tmp_path / "dup.py"
    file_path.write_text("print('x')\nprint('x')\n")

    result = tool.execute(
        file_path=str(file_path),
        old_text="print('x')",
        new_text="print('y')",
    )
    assert result.startswith("Error"), result
    assert "2" in result  # 命中数量提示
    # 原文未变
    assert file_path.read_text() == "print('x')\nprint('x')\n"


def test_no_match_error(tool, tmp_path):
    file_path = tmp_path / "no_match.py"
    file_path.write_text("foo\nbar\n")

    result = tool.execute(
        file_path=str(file_path),
        old_text="not_exist_text",
        new_text="anything",
    )
    assert result.startswith("Error"), result
    assert "未找到" in result


def test_append_mode_with_empty_old_text(tool, tmp_path):
    file_path = tmp_path / "log.txt"
    file_path.write_text("line1\n")

    result = tool.execute(
        file_path=str(file_path),
        old_text="",
        new_text="line2\n",
    )
    assert result.startswith("Success"), result
    text = file_path.read_text()
    assert text.endswith("line2\n")
    assert text.startswith("line1\n")


def test_file_missing_returns_error(tool, tmp_path):
    fake = tmp_path / "ghost.txt"
    result = tool.execute(
        file_path=str(fake),
        old_text="anything",
        new_text="x",
    )
    assert result.startswith("Error"), result
    assert "不存在" in result


def test_diff_output_contains_unified_markers(tool, tmp_path):
    file_path = tmp_path / "diff.txt"
    file_path.write_text("alpha\nbeta\n")
    result = tool.execute(
        file_path=str(file_path),
        old_text="beta",
        new_text="gamma",
    )
    assert "Success" in result
    # unified diff 头部
    assert "---" in result and "+++" in result or "--- diff ---" in result
    # 必须有 + 行或 - 行
    assert "\n+gamma" in result or "+gamma" in result


# ------------------------------------------------------------
# 安全层维度：体积超限 / 路径越界
# ------------------------------------------------------------

def test_security_blocks_oversize_new_text(isolated_security, tmp_path):
    """安全层 Layer 1: new_text 超过 200KB 被静态防御拒绝"""
    f = tmp_path / "target.txt"
    f.write_text("placeholder")

    huge = "A" * (201 * 1024)  # 201 KB
    allowed, msg = isolated_security.intercept("fuzzy_edit", {
        "file_path": str(f),
        "old_text": "placeholder",
        "new_text": huge,
    })
    assert allowed is False
    assert "超过上限" in (msg or "")


def test_security_blocks_path_jail_escape(isolated_security):
    """安全层 Layer 1: /etc/passwd 类越界路径必被拦"""
    allowed, msg = isolated_security.intercept("fuzzy_edit", {
        "file_path": "/etc/passwd",
        "old_text": "root",
        "new_text": "x",
    })
    assert allowed is False
    assert "Sandbox" in (msg or "") or "沙箱" in (msg or "")


def test_security_passes_in_sandbox_path(isolated_security, tmp_path):
    """安全层 Layer 1: 沙箱内文件应放行，并把 file_path 改写为绝对路径"""
    f = tmp_path / "ok.txt"
    f.write_text("hi")
    args = {
        "file_path": str(f),
        "old_text": "hi",
        "new_text": "yo",
    }
    allowed, msg = isolated_security.intercept("fuzzy_edit", args)
    assert allowed is True, msg
    # 入参被静默改写为绝对解析路径
    assert os.path.isabs(args["file_path"])
