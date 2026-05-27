"""
Harness-Lite CLI Application.
Command-line interface for the Harness-Lite multi-agent framework.
"""
import sys
import time
import typer
import asyncio
from typing import Optional

# ===== 引入 prompt_toolkit 核心组件（打造极致输入体验） =====
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory

# ===== 引入 rich 核心组件（实现 Claude Code 级别的富文本流式渲染） =====
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

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


class RichCLIOutputHandler:
    """基于 Token 聚合与 ANSI 反向清算技术的终端流式渲染拦截器（对标 Claude Code 稳压架构）。

    完美恢复 5 行内核作业面板的滚动设计，满了自动删除第一行。
    内置硬核的 ANSI 光标动态回溯算法，确保 Agent 调度外部物理工具时的所有中间碎碎念过渡句
    在正文最终出现前【从屏幕上完全物理蒸发抹消】。
    """

    def __init__(self):
        self.console = Console()
        self.status_lines = []  # 严格控制在 5 行以内的沙箱日志池
        self.thinking_live = None  # 轨道 1：5行瞬时动态滚动面板
        self.final_mode = False
        self.has_printed_prefix = False
        self.printed_text = ""  # 追踪本轮已喷涌出的文本内容
        self.newlines_count = 0  # 精准计入流式正文占据的物理行数

    def _get_thinking_renderable(self):
        """将 5 行滚动数据包装进一个固定美观的物理作业面板中。"""
        status_text_elements = []
        for line in self.status_lines:
            if "⚙️ 内核态" in line:
                status_text_elements.append(Text(line, style="cyan bold"))
            else:
                status_text_elements.append(Text(line, style="bright_black italic"))

        from rich.panel import Panel
        from rich.console import Group
        return Panel(
            Group(*status_text_elements),
            title="[bright_black]⚙️ 物理沙箱内核作业链 (Sandbox Kernel Operations)[/bright_black]",
            title_align="left",
            border_style="bright_black",
            padding=(0, 1)
        )

    def status_callback(self, content: str):
        """智能体在后台思考、并发调度物理工具链时的内核状态回调。"""
        clean_line = content.strip()
        if not clean_line or self.final_mode:
            # 最终结算轮作答结束后，彻底关闭任何后台干扰
            if self.final_mode and not self.thinking_live:
                return

        # ========================================================
        # 【🔥 核心黑科技：中间碎碎念完美物理蒸发】
        # 一旦检测到模型切入外部工具构造或并发调度的真实内核态，
        # 说明本轮之前流式喷涌出的文字 100% 属于中间过渡废话（碎碎念）。
        # 立刻触发 ANSI 逐行高敏反向回溯，将前缀与废话瞬间抹去，复原神圣、干净的终端！
        # ========================================================
        if (
                "调用外部能力" in clean_line or "正在并发调度工具" in clean_line or "执行中" in clean_line) and self.printed_text:
            sys.stdout.write("\r\033[K")  # 擦除当前行状态
            for _ in range(self.newlines_count):
                sys.stdout.write("\033[1A\033[K")  # 强力向上精准回溯并擦除碎碎念行
            sys.stdout.flush()

            # 状态深度自愈、数据全面重置
            self.printed_text = ""
            self.newlines_count = 0
            self.has_printed_prefix = False

        # 5行滚动沙箱状态面板的标准处理逻辑
        if clean_line.startswith("[🧠 思考中]"):
            text = clean_line.replace("[🧠 思考中]", "").strip()
            if text:
                if self.status_lines and self.status_lines[-1].startswith(" 🧠 思考中 ＞"):
                    if len(self.status_lines[-1]) > 80:
                        self.status_lines.append(f" 🧠 思考中 ＞ {text}")
                    else:
                        self.status_lines[-1] += text  # 词词增量无感累加
                else:
                    self.status_lines.append(f" 🧠 思考中 ＞ {text}")
        else:
            self.status_lines.append(f" ⚙️ 内核态 ＞ {clean_line}")

        while len(self.status_lines) > 5:
            self.status_lines.pop(0)

        if not self.thinking_live:
            self.thinking_live = Live(
                self._get_thinking_renderable(),
                console=self.console,
                transient=True,
                refresh_per_second=20
            )
            self.thinking_live.start()
        else:
            self.thinking_live.update(self._get_thinking_renderable())

    def stream_callback(self, content: str):
        """大模型正面回答正文时的流式直出回调。"""
        if not self.has_printed_prefix:
            # 临时关闭 5 行思考状态面板，为可能常驻的正文腾出空间
            if self.thinking_live:
                self.thinking_live.stop()
                self.thinking_live = None

            # 在绝对干净的屏幕行首打印全局唯一的常驻品红前缀
            self.console.print("[bold magenta]Harness-Lite ＞[/bold magenta]")
            sys.stdout.write("  ")
            self.has_printed_prefix = True
            self.newlines_count = 1  # 初始计入正文起始物理行

        # 留存历史足迹，用于 status_callback 严判清算
        self.printed_text += content
        self.newlines_count += content.count("\n")

        # 实时将 \n 替换为 \n  （让最终正文获得 2 格优雅对齐边距）
        formatted_chunk = content.replace("\n", "\n  ")

        # 律动打字机喷涌
        for char in formatted_chunk:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(0.015)

    def stop(self):
        """全链路大模型作答完毕后的闭环状态清理与自愈。"""
        if self.thinking_live:
            self.thinking_live.stop()
            self.thinking_live = None

        # 正式固化本轮交互，全面封锁清算逻辑
        self.final_mode = True
        sys.stdout.write("\n")
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
        handler = RichCLIOutputHandler()
        try:
            # 使用 try...finally 结构提供系统级视窗自愈机制
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
    """
    Run single-turn conversation (Used for single task command).
    """
    return asyncio.run(run_loop_async(task, session_id, stream))


@app.command()
def main(
        task: Optional[str] = typer.Argument(None, help="要执行的任务"),
        interactive: bool = typer.Option(False, "--interactive", "-i", help="交互模式"),
        session_id: Optional[str] = typer.Option(None, "--session", "-s", help="会话 ID")
):
    """
    Harness-Lite CLI: 现代大模型多智能体调度终端
    """
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

    # 交互模式：采用长生命周期单次运行的异步循环
    if interactive:
        asyncio.run(run_interactive_async(session_id_str))
        return

    # 单轮命令模式
    if task:
        response = run_loop(task, session_id_str, stream=True)
        return

    typer.echo("请提供要执行任务，或使用 --interactive 进入交互模式。")
    typer.echo("使用 --help 查看帮助信息。")


async def run_interactive_async(session_id: str) -> None:
    """
    长生命周期异步交互模式。

    完美整合非阻塞键盘事件监听与历史记录回溯。
    """
    typer.echo(f"(当前会话: {session_id}, 输入 'exit' 退出)")

    # 初始化输入 Session，内置内存级历史堆栈
    session = PromptSession(history=InMemoryHistory())

    # 采用带有 HTML 样式修饰的亮青色提示符，该前缀被 prompt_toolkit 强制锁定，绝不被退格键越界删除
    prompt_message = HTML("<ansicyan><b> ＞</b></ansicyan>")

    while True:
        try:
            # 严格环境清理：在唤起输入前清除缓冲区残余
            sys.stdout.flush()

            # 异步非阻塞等待用户输入，按上下方向键可调出历史命令
            user_input = await session.prompt_async(prompt_message)
            user_input = user_input.strip()

        except (EOFError, KeyboardInterrupt):
            typer.echo("\n再见！")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            typer.echo("再见！")
            break

        if not user_input:
            continue

        # 投递异步编排引擎流式运转
        await run_loop_async(user_input, session_id, stream=True)

        # 每一轮对话结束后，进行优雅的空行隔离
        typer.echo("")


if __name__ == "__main__":
    app()