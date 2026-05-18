"""Harness-Lite CLI Application.

Command-line interface for the Harness-Lite multi-agent framework.
"""
import sys
import time
from cgitb import handler

import typer
import asyncio
from typing import Optional

from harness_lite.config import get_llm_config
from harness_lite.loop import AsyncLoopEngine
from harness_lite.memory import MemoryManager

# Import tools and skills to trigger auto-registration
from harness_lite import tools
from harness_lite import skills

app = typer.Typer(
    name="harness-lite",
    help="Harness-Lite: 极简版大模型多智能体调度框架"
)


def generate_session_id() -> str:
    """Generate a unique session ID based on timestamp."""
    timestamp = int(time.time())
    return f"session-{timestamp}"

# 终端渲染拦截器
class CLIOutputHandler:
    def __init__(self):
        self.status_lines = []
        self.status_printed_count = 0
        self.prompt_printed = False

    def stream_callback(self, content: str):
        # 1. 擦除最后残留的状态日志（精确擦除）
        if self.status_printed_count > 0:
            for _ in range(self.status_printed_count):
                # \r 回到行首, \033[1A 上移一行, \033[2K 清空整行
                sys.stdout.write("\r\033[1A\033[2K")
            self.status_printed_count = 0
            self.status_lines.clear()

        # 2. 打印头部提示符（确保全局只打印一次）
        if not self.prompt_printed:
            sys.stdout.write("\rHarness-Lite > ")
            self.prompt_printed = True

        # 3. 输出大模型真正的回答内容
        for char in content:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(0.015)

    def status_callback(self, content: str):
        # 终极防御 1：强制剥离所有可能破坏格式的换行符，保证一行文本只占终端的一行
        clean_line = content.replace("\n", "").replace("\r", "").strip()
        if not clean_line:
            return

        # 终极防御 2：精确擦除旧的状态列表
        if self.status_printed_count > 0:
            for _ in range(self.status_printed_count):
                sys.stdout.write("\r\033[1A\033[2K")

        # 维持最多 5 行的滚动窗口
        self.status_lines.append(clean_line)
        if len(self.status_lines) > 5:
            self.status_lines.pop(0)

        # 重新打印最新的状态列表
        for line in self.status_lines:
            # 渲染成灰色并换行
            sys.stdout.write(f"\r\033[2K\033[90m{line}\033[0m\n")

        self.status_printed_count = len(self.status_lines)
        sys.stdout.flush()


def stream_output(content: str) -> None:
    """Stream output to stdout without newline, flush immediately."""
    sys.stdout.write(content)
    sys.stdout.flush()

async def run_loop_async(task: str, session_id: str, stream: bool = True) -> str:
    """
    Async core for running the loop.
    """
    engine = AsyncLoopEngine()
    if stream:
        handler = CLIOutputHandler()
        return await engine.run(
            task,
            session_id,
            stream_callback=handler.stream_callback,
            status_callback=handler.status_callback
        )
    else:
        return await engine.run(task, session_id)

def run_loop(task: str, session_id: str, stream: bool = True) -> str:
    """
    Run single-turn conversation.

    Args:
        task: User task
        session_id: Session ID
        stream: Whether to stream output

    Returns:
        LLM response
    """
    return asyncio.run(run_loop_async(task, session_id, stream))


@app.command()
def main(
    task: Optional[str] = typer.Argument(None, help="要执行的任务"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="交互模式"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="会话 ID")
):
    """
    Harness-Lite CLI

    示例：
        harness-lite "你好，介绍一下你自己"
        harness-lite --interactive
        harness-lite --session my_session "记住我喜欢蓝色"
    """
    # Initialize configuration first
    try:
        get_llm_config()
    except ValueError as e:
        typer.echo(f"配置错误: {e}", err=True)
        raise typer.Exit(code=1)

    # Handle session_id - use argument value directly since Option returns OptionInfo
    # We need to check sys.argv for the actual value
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

    # If interactive mode without explicit session, generate a new unique session
    if interactive and not session_id_str:
        session_id_str = generate_session_id()

    # Fallback to default if still None
    if not session_id_str:
        session_id_str = "default"

    # Interactive mode
    if interactive:
        run_interactive(session_id_str)
        return

    # Single-turn conversation
    if task:
        # Streaming output for single-turn mode
        typer.echo("Harness-Lite > ", nl=False)
        response = run_loop(task, session_id_str, stream=True)
        typer.echo("")
        return

    # No task provided in non-interactive mode
    typer.echo("请提供要执行的任务，或使用 --interactive 进入交互模式。")
    typer.echo("使用 --help 查看帮助信息。")


def run_interactive(session_id: str) -> None:
    """
    Run interactive conversation mode.

    Args:
        session_id: Session ID for memory management
    """
    # typer.echo(f"Harness-Lite > 你好！有什么可以帮你的？")
    typer.echo(f"(当前会话: {session_id}, 输入 'exit' 退出)")

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("\n再见！")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            typer.echo("再见！")
            break

        if not user_input:
            continue

        # Print prompt
        # typer.echo("Harness-Lite > ", nl=False)

        # Run the task with streaming
        response = run_loop(user_input, session_id, stream=True)

        # Ensure newline after streaming output
        typer.echo("")


if __name__ == "__main__":
    app()
