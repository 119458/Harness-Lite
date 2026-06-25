"""阶段 D 测试 - browser_automation 工具

约定：
- 不启动真实 Chromium：所有动作通过 mock BrowserRunner 桩件返回；
- 当环境缺 playwright 时 (`HAS_PLAYWRIGHT=False`) 整组测试 skip 不构成失败；
- Layer 2 语义审查只对 navigate 触发，验证 mock 调用次数；
- URL 黑名单 / action 白名单 / 中文错误提示。
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import harness_lite.tools  # noqa: F401
from harness_lite.tools import BrowserAutomationTool
from harness_lite.tools.browser_automation.browser_service import HAS_PLAYWRIGHT
from harness_lite.security.manager import SecurityManager


# ============================================================
# 基础结构 / Schema 测试（无需 playwright）
# ============================================================

def test_import_path_ok():
    """工具类可从顶层 harness_lite.tools 导入"""
    assert BrowserAutomationTool is not None


def test_tool_schema_lists_8_actions():
    tool = BrowserAutomationTool()
    schema = tool.get_schema()
    actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
    expected = {"navigate", "click", "fill", "scroll", "snapshot",
                "wait_for", "screenshot", "close"}
    assert set(actions) == expected, f"action 集合不一致: {actions}"


def test_tool_schema_required_only_action():
    tool = BrowserAutomationTool()
    schema = tool.get_schema()
    assert schema["function"]["parameters"]["required"] == ["action"]


# ============================================================
# 无 playwright 时所有 action 返回中文错误
# ============================================================

@pytest.mark.parametrize("action", [
    "navigate", "click", "fill", "scroll", "snapshot",
    "wait_for", "screenshot", "close",
])
def test_returns_chinese_error_when_playwright_missing(action, monkeypatch):
    """模拟 HAS_PLAYWRIGHT=False，无论哪个 action 都返回带中文的安装提示"""
    monkeypatch.setattr(
        "harness_lite.tools.browser_automation.browser_tool.HAS_PLAYWRIGHT",
        False,
    )
    tool = BrowserAutomationTool()
    # navigate 还需要 url
    kwargs = {"action": action}
    if action == "navigate":
        kwargs["url"] = "https://example.com"
    result = tool.execute(**kwargs)
    assert result.startswith("Error"), result
    assert "Playwright" in result or "playwright" in result
    assert "pip install" in result


def test_unknown_action_rejected():
    tool = BrowserAutomationTool()
    result = tool.execute(action="hack_me")
    assert result.startswith("Error")
    assert "action" in result


# ============================================================
# 安全层 Layer 1：navigate URL 黑名单、action 非法
# ============================================================

@pytest.fixture
def isolated_security(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return SecurityManager()


def test_security_blocks_blacklisted_navigate(isolated_security):
    """navigate 到 127.0.0.1 被 URL 黑名单拦截，绝不会到达 Layer 2"""
    with patch.object(isolated_security, "_llm_semantic_audit") as mock_layer2:
        allowed, msg = isolated_security.intercept("browser_automation", {
            "action": "navigate",
            "url": "http://127.0.0.1:8080/admin",
        })
    assert allowed is False
    assert "拦截" in (msg or "")
    # Layer 1 已挡掉，Layer 2 不应被调用
    mock_layer2.assert_not_called()


def test_security_blocks_invalid_action(isolated_security):
    allowed, msg = isolated_security.intercept("browser_automation", {
        "action": "drop_cookies",
    })
    assert allowed is False
    assert "action" in (msg or "")


def test_security_blocks_navigate_without_url(isolated_security):
    allowed, msg = isolated_security.intercept("browser_automation", {
        "action": "navigate",
    })
    assert allowed is False
    assert "url" in (msg or "")


def test_security_blocks_navigate_non_http(isolated_security):
    allowed, msg = isolated_security.intercept("browser_automation", {
        "action": "navigate",
        "url": "file:///etc/passwd",
    })
    assert allowed is False


# ============================================================
# Layer 2 仅对 navigate 触发：mock _llm_semantic_audit 验证调用次数
# ============================================================

def test_layer2_triggered_only_on_navigate(isolated_security):
    """非 navigate 动作不应触发 LLM 语义审查"""
    with patch.object(
        isolated_security,
        "_llm_semantic_audit",
        return_value=(100, "ok"),
    ) as mock_layer2:
        # snapshot / click / close / screenshot 都不应调用 layer2
        for action in ("snapshot", "click", "close", "screenshot", "scroll"):
            kwargs = {"action": action}
            if action == "click":
                kwargs["selector"] = "body"
            isolated_security.intercept("browser_automation", kwargs)

        assert mock_layer2.call_count == 0, (
            f"非 navigate 不应触发 Layer 2，但被调用 {mock_layer2.call_count} 次"
        )

        # 而 navigate 必触发
        isolated_security.intercept("browser_automation", {
            "action": "navigate",
            "url": "https://example.com",
        })
        assert mock_layer2.call_count == 1


# ============================================================
# 全程 mock 真实 BrowserRunner，验证工具能正确路由动作
# 这些测试不依赖 playwright，强制把 HAS_PLAYWRIGHT 设为 True，
# 然后用 mock runner 替代 _get_runner，避免触碰真实浏览器。
# ============================================================

@pytest.fixture
def mocked_runner_tool(monkeypatch):
    monkeypatch.setattr(
        "harness_lite.tools.browser_automation.browser_tool.HAS_PLAYWRIGHT",
        True,
    )
    tool = BrowserAutomationTool()
    fake_runner = MagicMock()
    fake_runner.navigate.return_value = {
        "url": "https://example.com",
        "title": "Demo",
        "status": 200,
    }
    fake_runner.snapshot.return_value = "Page: Demo\n---\n[ref:1] button"
    fake_runner.click.return_value = {"clicked": True}
    fake_runner.fill.return_value = {"filled": True}
    fake_runner.scroll.return_value = {"scrolled": "down", "amount": 500}
    fake_runner.wait_for.return_value = {"waited": True, "selector": "body"}
    fake_runner.screenshot.return_value = "/tmp/_browser_screenshots/snap.png"
    monkeypatch.setattr(tool, "_get_runner", lambda: fake_runner)
    return tool, fake_runner


def test_dispatch_navigate(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="navigate", url="https://example.com")
    assert "Demo" in result and "example.com" in result
    runner.navigate.assert_called_once()


def test_dispatch_navigate_rejects_non_http(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="navigate", url="file:///etc/passwd")
    assert result.startswith("Error")
    runner.navigate.assert_not_called()


def test_dispatch_click_requires_ref_or_selector(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="click")
    assert result.startswith("Error")
    runner.click.assert_not_called()


def test_dispatch_fill_requires_text(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="fill", selector="#q")
    assert result.startswith("Error")
    runner.fill.assert_not_called()


def test_dispatch_snapshot(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="snapshot")
    assert "Page:" in result
    runner.snapshot.assert_called_once()


def test_dispatch_screenshot(mocked_runner_tool):
    tool, runner = mocked_runner_tool
    result = tool.execute(action="screenshot")
    assert "截图已保存" in result
    runner.screenshot.assert_called_once()


# ============================================================
# Skip flag：当 playwright 真不可用时的标记示例
# ============================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="未安装 playwright，跳过依赖真实浏览器的集成检查")
def test_runner_singleton_returns_same_instance(tmp_path):
    """这是一个仅在装了 playwright 时跑的烟雾测试：BrowserRunner.get 单例"""
    from harness_lite.tools.browser_automation.browser_service import BrowserRunner
    r1 = BrowserRunner.get(tmp_path)
    r2 = BrowserRunner.get(tmp_path)
    assert r1 is r2
