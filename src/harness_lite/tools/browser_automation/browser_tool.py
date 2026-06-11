"""浏览器自动化工具：对 LLM 暴露 8 种动作"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from harness_lite.tools.base import BaseTool

from .browser_service import HAS_PLAYWRIGHT, BrowserRunner

_VALID_ACTIONS = {"navigate", "click", "fill", "scroll", "snapshot", "wait_for", "screenshot", "close"}
_DEFAULT_TIMEOUT_SECONDS = 30


class BrowserAutomationTool(BaseTool):
    """基于 Playwright Chromium 的浏览器自动化"""

    def __init__(self) -> None:
        super().__init__()
        self._runner: Optional[BrowserRunner] = None

    @property
    def name(self) -> str:
        return "browser_automation"

    @property
    def description(self) -> str:
        return (
            "浏览器自动化工具，基于 Chromium 提供页面导航、元素点击/填写、滚动、"
            "DOM 快照、等待元素、截图等能力。最佳实践：先用 snapshot 获取 [ref:N] 编号，"
            "再用 ref 字段定位 click/fill，避免依赖脆弱的 CSS 选择器。"
            "进程空闲 5 分钟会自动释放浏览器资源。"
        )

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "action": {
                "type": "string",
                "enum": sorted(_VALID_ACTIONS),
                "description": "操作类型：navigate=打开 URL，click=点击元素，fill=填写输入框，"
                               "scroll=滚动页面，snapshot=获取页面元素结构，wait_for=等待选择器出现，"
                               "screenshot=截图保存到沙箱，close=关闭浏览器释放资源。",
            },
            "url": {
                "type": "string",
                "description": "目标 URL，navigate 必填，必须以 http:// 或 https:// 开头。",
            },
            "ref": {
                "type": "integer",
                "description": "元素引用编号，来源于上一次 snapshot 输出的 [ref:N]；优先使用，比 selector 更稳。",
            },
            "selector": {
                "type": "string",
                "description": "CSS 选择器，作为 ref 的回退方案。click/fill/wait_for 可用。",
            },
            "text": {
                "type": "string",
                "description": "要填入输入框的文本，仅 fill 动作使用。",
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": "滚动方向，仅 scroll 动作使用，默认 down。",
            },
            "amount": {
                "type": "integer",
                "description": "滚动像素数，仅 scroll 动作使用，默认 500。",
            },
            "timeout": {
                "type": "integer",
                "description": f"操作超时秒数，默认 {_DEFAULT_TIMEOUT_SECONDS}，适用 navigate/click/fill/wait_for。",
            },
        }
        schema["function"]["parameters"]["required"] = ["action"]
        return schema

    def execute(self,
                action: str,
                url: Optional[str] = None,
                ref: Optional[int] = None,
                selector: Optional[str] = None,
                text: Optional[str] = None,
                direction: str = "down",
                amount: int = 500,
                timeout: int = _DEFAULT_TIMEOUT_SECONDS) -> str:
        if action not in _VALID_ACTIONS:
            return f"Error: 未知 action '{action}'，可选 {sorted(_VALID_ACTIONS)}。"

        if not HAS_PLAYWRIGHT:
            return ("Error: 当前环境未安装 Playwright，无法使用 browser_automation。"
                    "请先执行 `pip install playwright && playwright install chromium` 后重试。")

        timeout_ms = max(1, int(timeout)) * 1000

        try:
            runner = self._get_runner()
        except Exception as exc:  # noqa: BLE001
            return f"Error: 浏览器服务初始化失败（{exc}）。"

        try:
            return self._dispatch(runner, action, url, ref, selector, text, direction, amount, timeout_ms)
        except TimeoutError as exc:
            return f"Error: {exc}"
        except RuntimeError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: 浏览器操作异常（{exc}）。"

    # ---------- 内部 ----------

    def _get_runner(self) -> BrowserRunner:
        if self._runner is None:
            screenshot_dir = self._resolve_screenshot_dir()
            self._runner = BrowserRunner.get(screenshot_dir)
        return self._runner

    @staticmethod
    def _resolve_screenshot_dir() -> Path:
        try:
            from harness_lite.security.manager import security_manager
            base = security_manager.get_session_workspace("default")
        except Exception:  # noqa: BLE001
            base = Path.cwd()
        target = Path(base) / "_browser_screenshots"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _dispatch(self, runner: BrowserRunner, action: str, url: Optional[str],
                  ref: Optional[int], selector: Optional[str], text: Optional[str],
                  direction: str, amount: int, timeout_ms: int) -> str:
        if action == "navigate":
            if not url:
                return "Error: navigate 需要提供 url。"
            if not (url.startswith("http://") or url.startswith("https://")):
                return "Error: 仅支持 http/https URL。"
            result = runner.navigate(url, timeout_ms)
            return f"已打开页面：{result.get('title')} ({result.get('url')}) HTTP={result.get('status')}"

        if action == "snapshot":
            return runner.snapshot()

        if action == "click":
            if ref is None and not selector:
                return "Error: click 至少需要 ref 或 selector 之一。"
            return self._format_action_result(runner.click(ref, selector, timeout_ms))

        if action == "fill":
            if text is None:
                return "Error: fill 需要提供 text。"
            if ref is None and not selector:
                return "Error: fill 至少需要 ref 或 selector 之一。"
            return self._format_action_result(runner.fill(ref, selector, text, timeout_ms))

        if action == "scroll":
            return self._format_action_result(runner.scroll(direction, amount))

        if action == "wait_for":
            return self._format_action_result(runner.wait_for(selector, timeout_ms))

        if action == "screenshot":
            path = runner.screenshot()
            return f"截图已保存：{path}"

        if action == "close":
            runner.shutdown()
            self._runner = None
            return "已关闭浏览器并释放资源。"

        return f"Error: 不支持的 action '{action}'。"

    @staticmethod
    def _format_action_result(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return str(payload)
        if payload.get("error"):
            return f"Error: {payload['error']}"
        # 简洁可读
        parts = [f"{k}={v}" for k, v in payload.items()]
        return "OK | " + ", ".join(parts)
