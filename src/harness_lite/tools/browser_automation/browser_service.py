"""浏览器后台运行服务（基于 Playwright 同步 API + 单独工作线程）

为什么用后台线程？
- Playwright 的 sync_playwright 不允许跨线程调用，多线程访问会抛 GreenletExit。
- 单进程内统一通过一个工作线程串行执行，简化锁与生命周期管理。

调用方式：
- 外部调用 `BrowserRunner.submit(callable)`，runner 把 callable 入队，
  工作线程取出后在 Playwright 上下文里执行并把结果或异常回传。
- 长时间空闲（默认 300s）自动关闭浏览器释放资源。

如果环境没有 playwright，`HAS_PLAYWRIGHT` 为 False，所有调用会抛 RuntimeError，
上层工具捕获后返回中文提示。
"""

from __future__ import annotations

import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    HAS_PLAYWRIGHT = False

from .snapshot import DOM_SNAPSHOT_SCRIPT, flatten_snapshot

_IDLE_RELEASE_SECONDS = 300
_SUBMIT_TIMEOUT_SECONDS = 120
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BrowserRunner:
    """对外暴露的浏览器服务（线程安全单例）"""

    _instance: Optional["BrowserRunner"] = None
    _instance_lock = threading.Lock()

    def __init__(self, screenshot_dir: Path):
        self._screenshot_dir = Path(screenshot_dir)
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._task_queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._alive = False
        self._ready_event = threading.Event()
        self._state_lock = threading.Lock()
        self._idle_timer: Optional[threading.Timer] = None

        # Playwright 句柄，只在工作线程访问
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # ---------- 单例 ----------

    @classmethod
    def get(cls, screenshot_dir: Path) -> "BrowserRunner":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(screenshot_dir)
            return cls._instance

    # ---------- 生命周期 ----------

    def _ensure_worker(self) -> None:
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright 未安装，请执行 `pip install playwright && playwright install chromium`。")
        with self._state_lock:
            if self._alive and self._worker and self._worker.is_alive():
                return
            self._task_queue = queue.Queue()
            self._alive = True
            self._ready_event = threading.Event()
            self._worker = threading.Thread(
                target=self._worker_loop, name="HarnessBrowserWorker", daemon=True
            )
            self._worker.start()
        self._ready_event.wait(timeout=30)

    def _worker_loop(self) -> None:
        try:
            self._launch_browser()
        except Exception as exc:  # noqa: BLE001
            self._alive = False
            self._ready_event.set()
            self._drain_queue(RuntimeError(f"浏览器启动失败：{exc}"))
            return
        self._ready_event.set()

        while self._alive:
            try:
                item = self._task_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            fn, slot = item
            try:
                slot["result"] = fn()
            except Exception as exc:  # noqa: BLE001
                slot["error"] = exc
            finally:
                slot["done"].set()

        self._teardown_browser()

    def _launch_browser(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟，避免在没装包时报错

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._decide_headless(),
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        self._context = self._browser.new_context(
            viewport=_DEFAULT_VIEWPORT,
            user_agent=_DEFAULT_USER_AGENT,
        )
        self._page = self._context.new_page()

    @staticmethod
    def _decide_headless() -> bool:
        # 桌面环境（DISPLAY/WAYLAND_DISPLAY）跑有头，便于调试
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return False
        # macOS / Windows 默认有头
        if os.name == "nt":
            return False
        # Linux 服务器默认无头
        return True

    def _teardown_browser(self) -> None:
        for handle_name in ("_context", "_browser"):
            handle = getattr(self, handle_name, None)
            try:
                if handle:
                    handle.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(self, handle_name, None)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playwright = None
        self._page = None

    def _drain_queue(self, error: Exception) -> None:
        while True:
            try:
                item = self._task_queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            _, slot = item
            slot["error"] = error
            slot["done"].set()

    # ---------- 提交任务 ----------

    def submit(self, fn: Callable[[], Any]) -> Any:
        self._ensure_worker()
        if not self._alive:
            raise RuntimeError("浏览器服务不可用。")
        self._restart_idle_timer()

        slot: Dict[str, Any] = {"done": threading.Event()}
        self._task_queue.put((fn, slot))
        completed = slot["done"].wait(timeout=_SUBMIT_TIMEOUT_SECONDS)
        if not completed:
            raise TimeoutError(f"浏览器操作超过 {_SUBMIT_TIMEOUT_SECONDS} 秒未完成。")
        if "error" in slot:
            raise slot["error"]
        return slot.get("result")

    def shutdown(self) -> None:
        with self._state_lock:
            if not self._alive:
                return
            self._alive = False
            self._task_queue.put(None)
            worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=10)
        with self._state_lock:
            self._worker = None
        self._cancel_idle_timer()

    # ---------- 空闲自动释放 ----------

    def _restart_idle_timer(self) -> None:
        self._cancel_idle_timer()
        if _IDLE_RELEASE_SECONDS > 0:
            self._idle_timer = threading.Timer(_IDLE_RELEASE_SECONDS, self.shutdown)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    # ---------- 具体动作（都在工作线程里执行） ----------

    def navigate(self, url: str, timeout_ms: int) -> Dict[str, Any]:
        def _action():
            page = self._page
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            return {
                "url": page.url,
                "title": page.title(),
                "status": resp.status if resp else None,
            }
        return self.submit(_action)

    def snapshot(self) -> str:
        def _action():
            page = self._page
            result = page.evaluate(DOM_SNAPSHOT_SCRIPT)
            tree = result.get("tree") if isinstance(result, dict) else None
            ref_count = result.get("ref_count", 0) if isinstance(result, dict) else 0
            lines = flatten_snapshot(tree)
            head = (
                f"Page: {page.title()} ({page.url})\n"
                f"Interactive ref count: {ref_count}\n---"
            )
            return head + "\n" + "\n".join(lines)
        return self.submit(_action)

    def click(self, ref: Optional[int], selector: Optional[str], timeout_ms: int) -> Dict[str, Any]:
        def _action():
            page = self._page
            if ref is not None:
                return page.evaluate(
                    "([refId]) => {"
                    "  const el = window.__harnessRefMap && window.__harnessRefMap[refId];"
                    "  if (!el) return { error: 'ref ' + refId + ' 不存在，请先重新 snapshot。' };"
                    "  el.click();"
                    "  return { clicked: true, tag: el.tagName.toLowerCase() };"
                    "}",
                    [ref],
                )
            if selector:
                page.click(selector, timeout=timeout_ms)
                return {"clicked": True, "selector": selector}
            return {"error": "click 必须提供 ref 或 selector 之一。"}
        return self.submit(_action)

    def fill(self, ref: Optional[int], selector: Optional[str], text: str, timeout_ms: int) -> Dict[str, Any]:
        def _action():
            page = self._page
            if ref is not None:
                pre = page.evaluate(
                    "([refId]) => {"
                    "  const el = window.__harnessRefMap && window.__harnessRefMap[refId];"
                    "  if (!el) return { error: 'ref ' + refId + ' 不存在，请先 snapshot。' };"
                    "  el.focus();"
                    "  if ('value' in el) el.value = '';"
                    "  return { tag: el.tagName.toLowerCase() };"
                    "}",
                    [ref],
                )
                if isinstance(pre, dict) and pre.get("error"):
                    return pre
                page.keyboard.type(text)
                return {"filled": True, "ref": ref}
            if selector:
                page.fill(selector, text, timeout=timeout_ms)
                return {"filled": True, "selector": selector}
            return {"error": "fill 必须提供 ref 或 selector 之一。"}
        return self.submit(_action)

    def scroll(self, direction: str, amount: int) -> Dict[str, Any]:
        deltas = {
            "down": (0, amount),
            "up": (0, -amount),
            "right": (amount, 0),
            "left": (-amount, 0),
        }
        dx, dy = deltas.get(direction, (0, amount))

        def _action():
            self._page.mouse.wheel(dx, dy)
            self._page.wait_for_timeout(300)
            return {"scrolled": direction, "amount": amount}
        return self.submit(_action)

    def wait_for(self, selector: Optional[str], timeout_ms: int) -> Dict[str, Any]:
        def _action():
            page = self._page
            if selector:
                page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
                return {"waited": True, "selector": selector}
            page.wait_for_timeout(timeout_ms)
            return {"waited": True, "timeout_ms": timeout_ms}
        return self.submit(_action)

    def screenshot(self) -> str:
        def _action():
            file_path = self._screenshot_dir / f"snapshot_{uuid.uuid4().hex[:8]}.png"
            self._page.screenshot(path=str(file_path), full_page=False)
            return str(file_path)
        return self.submit(_action)
