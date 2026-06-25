"""阶段 D 测试 - doc_fetch 工具

覆盖：
- URL 协议校验 / file:// 拒绝
- 内网 IP 黑名单（127.0.0.1 / 10.x / 192.168.x / localhost / link-local）
- 缺少 pypdf / python-docx / openpyxl / python-pptx 时的中文错误提示
- 不支持的扩展名
- max_pages 越界
- 全程 mock requests.get / __import__，不发起真实网络请求
"""
from __future__ import annotations

import builtins
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import harness_lite.tools  # noqa: F401
from harness_lite.tools import DocFetchTool
from harness_lite.security.manager import SecurityManager


@pytest.fixture
def tool():
    return DocFetchTool()


@pytest.fixture
def isolated_security(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return SecurityManager()


# ============================================================
# Layer 1：URL / 协议 / 扩展名 / max_pages 校验（不发请求）
# ============================================================

def test_tool_rejects_non_http_url(tool):
    """工具内 _validate_url 拒绝非 http/https"""
    result = tool.execute(url="ftp://example.com/a.pdf")
    assert result.startswith("Error")
    assert "http" in result or "协议" in result


def test_tool_rejects_empty_url(tool):
    result = tool.execute(url="")
    assert result.startswith("Error")


def test_tool_rejects_unsupported_extension(tool):
    """非 PDF/Word/Excel/PPT 扩展名 -> 拒绝"""
    result = tool.execute(url="https://example.com/archive.zip")
    assert result.startswith("Error")
    assert "扩展" in result or "支持" in result


def test_security_blocks_file_scheme(isolated_security):
    allowed, msg = isolated_security.intercept("doc_fetch", {
        "url": "file:///etc/passwd",
        "max_pages": 1,
    })
    assert allowed is False
    assert "Security" in (msg or "") or "拦截" in (msg or "") or "http" in (msg or "")


@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1/foo.pdf",
    "https://localhost/foo.pdf",
    "http://10.0.0.5/x.pdf",
    "http://192.168.1.1/x.pdf",
    "http://172.16.0.1/x.pdf",
    "http://169.254.0.1/x.pdf",  # link-local
    "http://0.0.0.0/x.pdf",
])
def test_security_blocks_internal_urls(isolated_security, bad_url):
    """各类内网 / localhost / link-local 地址必须被黑名单拦截"""
    allowed, msg = isolated_security.intercept("doc_fetch", {
        "url": bad_url,
        "max_pages": 1,
    })
    assert allowed is False, f"未拦截内网地址: {bad_url}"
    assert "拦截" in (msg or "") or "黑名单" in (msg or "")


def test_security_blocks_max_pages_over_limit(isolated_security):
    """max_pages > 500 应被安全层拒绝"""
    allowed, msg = isolated_security.intercept("doc_fetch", {
        "url": "https://example.com/a.pdf",
        "max_pages": 9999,
    })
    assert allowed is False
    assert "max_pages" in (msg or "")


def test_security_blocks_max_pages_zero(isolated_security):
    allowed, msg = isolated_security.intercept("doc_fetch", {
        "url": "https://example.com/a.pdf",
        "max_pages": 0,
    })
    assert allowed is False


def test_security_passes_valid_public_pdf(isolated_security):
    allowed, _ = isolated_security.intercept("doc_fetch", {
        "url": "https://example.com/a.pdf",
        "max_pages": 5,
    })
    assert allowed is True


# ============================================================
# Mock 依赖缺失场景：不真的 pip 卸载，通过拦截 __import__ 模拟
# ============================================================

def _make_import_blocker(blocked_modules):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in blocked_modules:
            raise ImportError(f"mocked: no module named {name!r}")
        return real_import(name, globals, locals, fromlist, level)

    return fake_import


@pytest.mark.parametrize("missing_module,extension,expected_keyword", [
    ("pypdf", ".pdf", "pypdf"),
    ("docx", ".docx", "python-docx"),
    ("openpyxl", ".xlsx", "openpyxl"),
    ("pptx", ".pptx", "python-pptx"),
])
def test_missing_parser_returns_chinese_error(tool, tmp_path, missing_module, extension, expected_keyword):
    """缺少特定解析依赖时必须返回带中文提示的错误，并提示安装命令"""
    # 准备：mock requests 让"下载"成功（写一个空白文件）
    fake_resp = MagicMock()
    fake_resp.headers = {}
    fake_resp.raise_for_status = MagicMock()
    # iter_content 返回单块假数据
    fake_resp.iter_content = MagicMock(return_value=[b"%PDF-1.4\n%fake"])
    fake_requests = MagicMock()
    fake_requests.get = MagicMock(return_value=fake_resp)
    fake_requests.Timeout = type("Timeout", (Exception,), {})
    fake_requests.ConnectionError = type("ConnectionError", (Exception,), {})
    fake_requests.HTTPError = type("HTTPError", (Exception,), {})

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "requests":
            return fake_requests
        if name == missing_module:
            raise ImportError(f"mocked: no module named {name!r}")
        return real_import(name, globals, locals, fromlist, level)

    url = f"https://example.com/sample{extension}"
    with patch("builtins.__import__", side_effect=fake_import):
        result = tool.execute(url=url, max_pages=1)

    assert result.startswith("Error"), result
    assert expected_keyword in result, f"未提示 {expected_keyword}: {result}"
    assert "pip install" in result


def test_missing_requests_returns_chinese_error(tool):
    """连 requests 都没装时也要给出中文兜底错误"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "requests":
            raise ImportError("mocked")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = tool.execute(url="https://example.com/a.pdf", max_pages=1)

    assert result.startswith("Error")
    assert "requests" in result
    assert "pip install" in result


# ============================================================
# Schema 校验
# ============================================================

def test_schema_required_url(tool):
    schema = tool.get_schema()
    assert schema["function"]["name"] == "doc_fetch"
    params = schema["function"]["parameters"]
    assert "url" in params["properties"]
    assert "max_pages" in params["properties"]
    assert "url" in params["required"]
