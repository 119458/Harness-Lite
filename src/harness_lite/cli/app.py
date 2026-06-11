"""Harness-Lite CLI."""
import sys
import os
import time
import typer
import asyncio
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text
from rich.panel import Panel

from harness_lite.config import get_llm_config
from harness_lite.loop import AsyncLoopEngine
from harness_lite.memory import MemoryManager

from harness_lite import tools
from harness_lite import skills

app = typer.Typer(
    name="harness-lite",
    help="Harness-Lite: 多智能体编排 CLI"
)


# 上游 status_callback 推送的 6 个 prefix token → 短标签映射
# 上游来源: loop/strategy.py:167,190,215,345,388  loop/engine.py:349,365
STATUS_PREFIXES = {
    "[🧠 思考中]": "thinking",
    "[⚙️ 线程激活]": "tool",
    "[✅ 已完成]": "done",
    "[💾 上下文压缩]": "compact",
    "[⚠️ 状态自愈]": "recover",
    "[⚠️ 纠错中]": "retry",
}


class CommandCompleter(Completer):
    def __init__(self):
        self.commands = {
            "/model": "查看 LLM 服务配置",
            "/tool": "列出已注册工具",
            "/skill": "列出已加载技能",
            "/mem0": "切换 mem0 长记忆",
            "/clear": "清空当前会话上下文",
            "/session": "查看会话与沙箱信息",
            "/sandbox": "动态挂载沙箱目录（多路径以空格分隔）",
            "/exit": "退出 CLI",
        }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # /sandbox 后的目录补全
        if text.startswith("/sandbox "):
            parts = text.split(" ")
            current_fragment = parts[-1]
            if current_fragment.endswith("/") or current_fragment.endswith("\\"):
                dirname = current_fragment
                basename = ""
            else:
                dirname = os.path.dirname(current_fragment)
                basename = os.path.basename(current_fragment)
                if current_fragment in (".", ".."):
                    dirname = current_fragment
                    basename = ""

            scan_dir = os.path.abspath(dirname) if dirname else os.getcwd()
            if os.path.isdir(scan_dir):
                try:
                    for entry in os.listdir(scan_dir):
                        if entry.startswith(".") and not basename.startswith("."):
                            continue
                        full_path = os.path.join(scan_dir, entry)
                        if os.path.isdir(full_path) and entry.startswith(basename):
                            if dirname:
                                completion_text = f"{dirname.rstrip('/')}/{entry}/"
                            else:
                                completion_text = f"{entry}/"
                            yield Completion(
                                text=completion_text,
                                start_position=-len(current_fragment),
                                display_meta="Directory"
                            )
                except Exception:
                    pass
            return

        # 斜杠基础命令补全
        if text.startswith("/"):
            for cmd, desc in self.commands.items():
                if cmd.startswith(text):
                    completion_text = f"{cmd} " if cmd == "/sandbox" else cmd
                    yield Completion(
                        text=completion_text,
                        start_position=-len(text),
                        display_meta=desc
                    )


# 低饱和补全菜单样式
tui_style = Style.from_dict({
    "completion-menu.completion": "bg:#1a1a1a #d0d0d0",
    "completion-menu.completion.current": "bg:#005577 #ffffff bold",
    "completion-menu.meta.completion": "bg:#1a1a1a #707070 italic",
    "completion-menu.meta.completion.current": "bg:#005577 #ffffff italic",
})


def _panel(content, title: str) -> Panel:
    """统一的细线灰边框面板。"""
    return Panel(
        content,
        title=f"[cyan]{title}[/cyan]",
        title_align="left",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 1),
        expand=False,
    )


async def handle_slash_command(command_str: str, session_id: str, engine: AsyncLoopEngine) -> bool:
    """本地斜杠命令路由。"""
    parts = command_str.split()
    cmd = parts[0].lower()
    console = Console()

    if cmd == "/exit":
        console.print("[dim]Bye.[/dim]")
        return True

    elif cmd == "/model":
        try:
            config = get_llm_config()
            thinking = "enabled" if config.get('thinking_mode') else "disabled"
            content = (
                f"[cyan]model[/cyan]    : {config.get('model_name')}\n"
                f"[cyan]base_url[/cyan] : {config.get('base_url')}\n"
                f"[cyan]thinking[/cyan] : {thinking}"
            )
            console.print(_panel(content, "Model"))
        except Exception as e:
            console.print(f"[red]error:[/red] failed to load model config: {e}")

    elif cmd in ("/tool", "/tools"):
        from harness_lite.registry.tool_registry import tool_registry
        all_tools = tool_registry.list_all()
        if not all_tools:
            console.print("[dim]no tools registered.[/dim]")
        else:
            lines = []
            for idx, t in enumerate(all_tools, 1):
                lines.append(
                    f"[dim]{idx:02d}[/dim]  [cyan]{t['name']}[/cyan]   {t['description']}"
                )
            console.print(_panel("\n".join(lines), f"Tools ({len(all_tools)})"))

    elif cmd == "/skill":
        from harness_lite.registry.skill_registry import skill_registry
        all_skills = skill_registry.list_all()
        if not all_skills:
            console.print("[dim]no skills loaded.[/dim]")
        else:
            lines = []
            for idx, sk in enumerate(all_skills, 1):
                lines.append(
                    f"[dim]{idx:02d}[/dim]  [cyan]{sk.name}[/cyan]   {sk.description}"
                )
            console.print(_panel("\n".join(lines), f"Skills ({len(all_skills)})"))

    elif cmd == "/mem0":
        try:
            status_msg = engine.memory.toggle_mem0()
            console.print(status_msg)
        except Exception as e:
            console.print(f"[red]error:[/red] mem0 toggle failed: {e}")

    elif cmd == "/clear":
        try:
            engine.memory.clear_context(session_id)
            console.print(f"[cyan]✓[/cyan] context cleared [dim](session={session_id})[/dim]")
        except Exception as e:
            console.print(f"[red]error:[/red] clear failed: {e}")

    elif cmd == "/sandbox":
        from harness_lite.security.manager import security_manager
        if len(parts) < 2:
            roots = sorted(security_manager.active_sandbox_roots)
            if not roots:
                console.print("[dim]no sandbox mounted.[/dim]")
            else:
                lines = [f"  - {r}" for r in roots]
                console.print(_panel("\n".join(lines), "Sandboxes"))
        else:
            sandbox_paths = parts[1:]
            try:
                security_manager.set_active_sandboxes(sandbox_paths)
                roots = sorted(security_manager.active_sandbox_roots)
                lines = [f"  - {r}" for r in roots]
                console.print(_panel("\n".join(lines), "Sandboxes (updated)"))
            except Exception as e:
                console.print(f"[red]error:[/red] sandbox mount failed: {e}")

    elif cmd == "/session":
        from harness_lite.security.manager import security_manager
        roots = sorted(security_manager.active_sandbox_roots)
        roots_desc = "\n".join([f"  - [dim]{r}[/dim]" for r in roots]) or "  [dim](none)[/dim]"
        content = (
            f"[cyan]session_id[/cyan] : {session_id}\n"
            f"[cyan]sandboxes[/cyan]  :\n{roots_desc}"
        )
        console.print(_panel(content, "Session"))

    else:
        console.print(f"[red]unknown command:[/red] {cmd}")

    return False


def generate_session_id() -> str:
    """产生时间戳会话 ID。"""
    timestamp = int(time.time())
    return f"session-{timestamp}"


def _strip_status_prefix(line: str) -> tuple[str, str]:
    """从状态行提取 (label, body)。未知前缀返回 ('info', 原行)。"""
    for prefix, label in STATUS_PREFIXES.items():
        if line.startswith(prefix):
            return label, line[len(prefix):].strip()
    return "info", line


class RichCLIOutputHandler:
    """极简流式渲染：cyan 强调色 + dim 状态行 + 无装饰。"""

    def __init__(self):
        self.console = Console()
        self.status_lines = []
        self.thinking_live = None
        self.final_mode = False
        self.has_printed_prefix = False
        self.printed_text = ""
        self.newlines_count = 0

    def _get_thinking_renderable(self) -> Group:
        """无 Panel 包裹，直接返回缩进行 Group。"""
        elements = []
        for label, body in self.status_lines:
            line = Text()
            line.append("  · ", style="dim")
            line.append(f"{label:<8}", style="cyan")
            line.append(body)
            elements.append(line)
        return Group(*elements)

    def status_callback(self, content: str):
        clean_line = content.strip()
        if not clean_line:
            return
        if self.final_mode and not self.thinking_live:
            return

        label, body = _strip_status_prefix(clean_line)

        # tool 启动是真正的工具开始信号：擦除已打印的中间文本
        if label == "tool" and self.printed_text:
            sys.stdout.write("\r\033[K")
            for _ in range(self.newlines_count):
                sys.stdout.write("\033[1A\033[K")
            sys.stdout.flush()
            self.printed_text = ""
            self.newlines_count = 0
            self.has_printed_prefix = False

        if label == "thinking":
            # 仅在纯思考阶段使用 Live 动画合并输出
            if self.status_lines and self.status_lines[-1][0] == "thinking":
                last_label, last_body = self.status_lines[-1]
                if len(last_body) > 85:
                    self.status_lines.append((label, body))
                else:
                    self.status_lines[-1] = (last_label, last_body + body)
            else:
                self.status_lines.append((label, body))

            while len(self.status_lines) > 5:
                self.status_lines.pop(0)

            if not self.thinking_live:
                self.thinking_live = Live(
                    self._get_thinking_renderable(),
                    console=self.console,
                    transient=True,
                    refresh_per_second=30,
                )
                self.thinking_live.start()
            else:
                self.thinking_live.update(self._get_thinking_renderable())

        else:
            # 遇到非 thinking 状态（如 tool, recover 等），必须立即停止后台重绘线程！
            if self.thinking_live:
                self.thinking_live.stop()
                self.thinking_live = None

            # 以纯静态的文本打印当前动作，不开启任何后台线程，从而释放标准输出的控制权
            self.console.print(f"  [dim]·[/dim] [cyan]{label:<8}[/cyan] {body}")

            # 仍记录到历史队列，确保后续如果又开始 thinking，这个上下文能被一起带入并正确缩进显示
            self.status_lines.append((label, body))
            while len(self.status_lines) > 5:
                self.status_lines.pop(0)

    def stream_callback(self, content: str):
        if not self.has_printed_prefix:
            if self.thinking_live:
                self.thinking_live.stop()
                self.thinking_live = None

            self.console.print("[cyan]助手:[/cyan]")
            sys.stdout.write("  ")
            self.has_printed_prefix = True
            self.newlines_count = 1

        self.printed_text += content
        self.newlines_count += content.count("\n")

        formatted_chunk = content.replace("\n", "\n  ")

        for char in formatted_chunk:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(0.003)

    def stop(self):
        if self.thinking_live:
            self.thinking_live.stop()
            self.thinking_live = None

        self.final_mode = True
        sys.stdout.write("\n")
        sys.stdout.flush()


async def run_loop_async(task: str, session_id: str, stream: bool = True,
                         engine: Optional[AsyncLoopEngine] = None) -> str:
    """运行一次循环。"""
    if engine is None:
        engine = AsyncLoopEngine()

    if stream:
        handler = RichCLIOutputHandler()
        try:
            return await engine.run(
                task,
                session_id,
                stream_callback=handler.stream_callback,
                status_callback=handler.status_callback
            )
        finally:
            handler.stop()
    else:
        return await engine.run(task, session_id)


def run_loop(task: str, session_id: str, stream: bool = True) -> str:
    return asyncio.run(run_loop_async(task, session_id, stream))


@app.command()
def main(
        task: Optional[str] = typer.Argument(None, help="要执行的任务"),
        interactive: bool = typer.Option(False, "--interactive", "-i", help="交互模式"),
        session_id: Optional[str] = typer.Option(None, "--session", "-s", help="会话 ID")
):
    """Harness-Lite: 多智能体编排 CLI"""
    try:
        get_llm_config()
    except ValueError as e:
        typer.echo(f"配置错误: {e}", err=True)
        raise typer.Exit(code=1)

    import sys
    session_id_str = None
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        if idx + 1 < len(sys.argv):
            session_id_str = sys.argv[idx + 1]
    elif "-s" in sys.argv:
        idx = sys.argv.index("-s")
        if idx + 1 < len(sys.argv):
            session_id_str = sys.argv[idx + 1]

    if interactive and not session_id_str:
        session_id_str = generate_session_id()

    if not session_id_str:
        session_id_str = "default"

    if interactive:
        asyncio.run(run_interactive_async(session_id_str))
        return

    if task:
        run_loop(task, session_id_str, stream=True)
        return

    typer.echo("请提供要执行的任务，或使用 --interactive / -i 进入交互模式。")


async def run_interactive_async(session_id: str) -> None:
    """交互模式：长生命周期会话。"""
    Console().print(f"[dim]session={session_id}  ·  type /exit to quit[/dim]")

    # 单实例 engine，使 memory.use_mem0 等状态跨多轮保留
    global_engine = AsyncLoopEngine()
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()

    @kb.add('enter')
    def _(event):
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.complete_state = None
        else:
            buffer.validate_and_handle()

    session = PromptSession(
        history=InMemoryHistory(),
        completer=CommandCompleter(),
        style=tui_style,
        complete_while_typing=True,
        key_bindings=kb,
    )
    prompt_message = HTML("<ansicyan>❯ </ansicyan>")

    while True:
        try:
            sys.stdout.flush()
            user_input = await session.prompt_async(prompt_message)
            user_input = user_input.strip()

        except (EOFError, KeyboardInterrupt):
            Console().print("[dim]Bye.[/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            should_exit = await handle_slash_command(user_input, session_id, global_engine)
            if should_exit:
                break
            typer.echo("")
            continue

        await run_loop_async(user_input, session_id, stream=True, engine=global_engine)

        typer.echo("")


if __name__ == "__main__":
    app()
