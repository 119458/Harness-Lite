"""Harness-Lite CLI Application.

Command-line interface for the Harness-Lite multi-agent framework.
"""
import sys
import time
import typer
from typing import Optional

from harness_lite.config import get_llm_config
from harness_lite.loop import LoopEngine
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


def stream_output(content: str) -> None:
    """Stream output to stdout without newline, flush immediately."""
    sys.stdout.write(content)
    sys.stdout.flush()


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
    engine = LoopEngine(session_id=session_id)
    callback = stream_output if stream else None
    return engine.run(task, session_id, stream_callback=callback)


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
    typer.echo(f"Harness-Lite > 你好！有什么可以帮你的？")
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
        typer.echo("Harness-Lite > ", nl=False)

        # Run the task with streaming
        response = run_loop(user_input, session_id, stream=True)

        # Ensure newline after streaming output
        typer.echo("")


if __name__ == "__main__":
    app()
