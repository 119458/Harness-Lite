"""
Harness-Lite CLI Application.
赛博矩阵平衡版：霓虹战术面板与极简输入提示符的完美撞色融合。
"""
import sys
import time
import typer
import asyncio
from typing import Optional

# ===== prompt_toolkit 核心组件（打造极致输入与霓虹弹出菜单） =====
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style

# ===== rich 核心组件（赛博朋克 TUI 富文本重绘战术面板） =====
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from rich.panel import Panel

from harness_lite.config import get_llm_config
from harness_lite.loop import AsyncLoopEngine
from harness_lite.memory import MemoryManager

# 触发原子工具与技能的自动化注册
from harness_lite import tools
from harness_lite import skills

app = typer.Typer(
    name="harness-lite",
    help="⚡ HARNESS-LITE: 量子神经元多智能体编排矩阵控制台"
)

# ========================================================
# 🎨 赛博朋克核心调色盘 (Cyberpunk Palettes)
# 🖥️ #00f0ff (荧光青) | #ff0055 (霓虹粉) | #fdee21 (警告黄)
# ========================================================

class CyberCommandCompleter(Completer):
    """赛博朋克专属：黑客指令动态补全菜单（纯中文释义）"""
    def __init__(self):
        self.commands = {
            "/model": "🧬 [核心内核] 探测当前量子神经元大脑模型配置",
            "/tool": "⚡ [外置义体] 扫描当前接入沙箱的所有原子工具链",
            "/skill": "📚 [知识芯片] 读取全量常驻 SOP 技能芯片库",
            "/clear": "🧠 [意识净化] 核心级热重置，洗涤短期交互历史链",
            "/session": "🌐 [上行视窗] 定位当前加密隔离工作区安全边界",
            "/exit": "🚨 退出当前cli",
        }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd, desc in self.commands.items():
                if cmd.startswith(text):
                    yield Completion(
                        text=cmd,
                        start_position=-len(text),
                        display_meta=desc
                    )

# 暗黑底色 + 高饱和度霓虹粉/荧光青高反差弹出菜单样式
cyber_tui_style = Style.from_dict({
    "completion-menu.completion": "bg:#121214 #00f0ff",
    "completion-menu.completion.current": "bg:#ff0055 #ffffff bold",
    "completion-menu.meta.completion": "bg:#121214 #888888 italic",
    "completion-menu.meta.completion.current": "bg:#ff0055 #ffffff italic",
})

async def handle_slash_command(command_str: str, session_id: str) -> bool:
    """赛博浪人本地指令路由拦截器（全中文回显）"""
    parts = command_str.split()
    cmd = parts[0].lower()
    console = Console()

    if cmd == "/exit":
        console.print("\n[bold #ff0055]🚨 退出当前cli[/bold #ff0055]")
        return True

    elif cmd == "/model":
        try:
            config = get_llm_config()
            panel_content = (
                f"[#00f0ff]» 核心引擎驱动:[/#00f0ff] [green]{config.get('model_name')}[/green]\n"
                f"[#00f0ff]» 神经网关端点:[/#00f0ff] [bright_black]{config.get('base_url')}[/bright_black]\n"
                f"[#00f0ff]» 深度认知模式:[/#00f0ff] " +
                ("[bold #fdee21]已开启 (大模型思维链思考流)[/bold #fdee21]" if config.get('thinking_mode') else "[bright_black]标准推理[/bright_black]")
            )
            console.print(Panel(panel_content, title="[#ff0055]◢ 量子核心膜状态膜 ◤[/#ff0055]", border_style="#ff0055", expand=False))
        except Exception as e:
            console.print(f"[bold red]❌ [高危崩溃] 神经元配置装载失败: {e}[/bold red]")

    elif cmd in ("/tool", "/tools"):
        from harness_lite.registry.tool_registry import tool_registry
        all_tools = tool_registry.list_all()
        if not all_tools:
            console.print("[#fdee21]⚠️ [警戒] 当前外部物理沙箱未检测到任何外置义体工具。[/#fdee21]")
        else:
            lines = []
            for idx, t in enumerate(all_tools, 1):
                lines.append(f"[#00f0ff][{idx:02d}][/#00f0ff] [bold green]󰡦 {t['name']}[/bold green] ── [bright_black]{t['description']}[/bright_black]")
            console.print(Panel("\n".join(lines), title="[#ff0055]◢ 活跃外置增强义体工具箱 ◤[/#ff0055]", border_style="#00f0ff", expand=False))

    elif cmd == "/skill":
        from harness_lite.registry.skill_registry import skill_registry
        all_skills = skill_registry.list_all()
        if not all_skills:
            console.print("[#fdee21]⚠️ [警戒] 常驻技能芯片库目前处于空载状态。[/#fdee21]")
        else:
            lines = []
            for idx, sk in enumerate(all_skills, 1):
                lines.append(f"[#ff0055][{idx:02d}][/#ff0055] [bold #fdee21]💾 {sk.name}[/bold #fdee21] ── [bright_black]{sk.description}[/bright_black]")
            console.print(Panel("\n".join(lines), title="[#00f0ff]◢ 常驻 SOP 技能芯片索引库 ◤[/#00f0ff]", border_style="#ff0055", expand=False))

    elif cmd == "/clear":
        try:
            # 严格贯彻职责分离：业务逻辑全权交由 MemoryManager 热重置
            memory_manager = MemoryManager()
            memory_manager.clear_context(session_id)
            console.print(f"[bold #00f0ff]🧠 [意识净化完成] 认知记忆向量已归零，短期历史流成功截断。[/bold #00f0ff]")
            console.print("  [#888888]↳ 内核底座保持常驻：安全沙箱边界、核心 System 设置及外置工具 Schema 已被强制锁死留存。[/#888888]")
        except Exception as e:
            console.print(f"[bold red]❌ [重置失败] 记忆重启中途遭遇硬代码异常: {e}[/bold red]")

    elif cmd == "/session":
        panel_content = (
            f"[#00f0ff]» 隔离矩阵路径:[/#00f0ff] [green]session://{session_id}[/green]\n"
            f"[#00f0ff]» 沙箱安全边界:[/#00f0ff] [bright_black]sandbox/session_{session_id}/work/[/bright_black]"
        )
        console.print(Panel(panel_content, title="[#fdee21]◢ 加密上行活跃节点详情 ◤[/#fdee21]", border_style="#00f0ff", expand=False))

    else:
        console.print(f"[bold red]❌ [语法错误] 未识别的矩阵指令: {cmd}。请按下 '/' 调出赛博指令菜单。[/bold red]")

    return False


def generate_session_id() -> str:
    """产生唯一的时间戳会话标记。"""
    timestamp = int(time.time())
    return f"cyber-{timestamp}"


class RichCLIOutputHandler:
    """对标 Claude Code 稳压架构的纯中文赛博机甲风流式拦截渲染器。"""

    def __init__(self):
        self.console = Console()
        self.status_lines = []  # 严格限制在 5 行内的机甲作业缓冲区
        self.thinking_live = None
        self.final_mode = False
        self.has_printed_prefix = False
        self.printed_text = ""
        self.newlines_count = 0

    def _get_thinking_renderable(self):
        """组装具备强烈战术工业质感的纯中文控制面板"""
        status_text_elements = []
        for line in self.status_lines:
            if "⚡ 矩阵偏置" in line:
                status_text_elements.append(Text(line, style="#fdee21 bold"))
            else:
                status_text_elements.append(Text(line, style="#00f0ff italic"))

        return Panel(
            Group(*status_text_elements),
            title="[bold #ff0055]⚡ 赛博控制台矩阵作业流 // ⚙️[/bold #ff0055]",
            title_align="left",
            border_style="#ff0055",
            padding=(0, 1)
        )

    def status_callback(self, content: str):
        clean_line = content.strip()
        if not clean_line or self.final_mode:
            if self.final_mode and not self.thinking_live:
                return

        # ========================================================
        # 【🔥 赛博美学纠错：中间过渡废话瞬间物理蒸发】
        # ========================================================
        if ("调用外部能力" in clean_line or "正在并发调度工具" in clean_line or "执行中" in clean_line) and self.printed_text:
            sys.stdout.write("\r\033[K")
            for _ in range(self.newlines_count):
                sys.stdout.write("\033[1A\033[K")
            sys.stdout.flush()

            self.printed_text = ""
            self.newlines_count = 0
            self.has_printed_prefix = False

        if clean_line.startswith("[🧠 思考中]"):
            text = clean_line.replace("[🧠 思考中]", "").strip()
            if text:
                if self.status_lines and self.status_lines[-1].startswith(" 🧠 神经元认知 ❯"):
                    if len(self.status_lines[-1]) > 85:
                        self.status_lines.append(f" 🧠 神经元认知 ❯ {text}")
                    else:
                        self.status_lines[-1] += text
                else:
                    self.status_lines.append(f" 🧠 神经元认知 ❯ {text}")
        else:
            cyber_kernel_msg = (
                clean_line.replace("调用外部能力", "接入外部增强义体工具")
                          .replace("正在并发调度工具", "启动多路并行调度矩阵")
                          .replace("执行中", "线程动态激活执行中")
            )
            self.status_lines.append(f" ⚡ 矩阵偏置 ❯ {cyber_kernel_msg}")

        while len(self.status_lines) > 5:
            self.status_lines.pop(0)

        if not self.thinking_live:
            self.thinking_live = Live(
                self._get_thinking_renderable(),
                console=self.console,
                transient=True,
                refresh_per_second=30
            )
            self.thinking_live.start()
        else:
            self.thinking_live.update(self._get_thinking_renderable())

    def stream_callback(self, content: str):
        """大模型正面吐字输出时的流式优雅边距对齐回调"""
        if not self.has_printed_prefix:
            if self.thinking_live:
                self.thinking_live.stop()
                self.thinking_live = None

            # 顶级高压霓虹粉中文流式交互前缀
            self.console.print("[bold #ff0055]⚡ 智能体回应 // ❯[/bold #ff0055]")
            sys.stdout.write("  ")
            self.has_printed_prefix = True
            self.newlines_count = 1

        self.printed_text += content
        self.newlines_count += content.count("\n")

        formatted_chunk = content.replace("\n", "\n  ")

        for char in formatted_chunk:
            sys.stdout.write(char)
            sys.stdout.flush()
            time.sleep(0.012)

    def stop(self):
        if self.thinking_live:
            self.thinking_live.stop()
            self.thinking_live = None

        self.final_mode = True
        sys.stdout.write("\n")
        sys.stdout.flush()


def stream_output(content: str) -> None:
    sys.stdout.write(content)
    sys.stdout.flush()


async def run_loop_async(task: str, session_id: str, stream: bool = True) -> str:
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
    """
    ⚡ Harness-Lite: 赛博多智能体编排调度终端 / 极客黑客交互视窗
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

    if interactive:
        asyncio.run(run_interactive_async(session_id_str))
        return

    if task:
        response = run_loop(task, session_id_str, stream=True)
        return

    typer.echo("请提供要执行的任务，或使用 --interactive / -i 进入长生命周期交互模式。")


async def run_interactive_async(session_id: str) -> None:
    """长生命周期暗网黑客多路异步会话模式"""
    # 🔗 【对齐闭环】提示文案与底层的 /exit 脱机操作保持完全一致
    Console().print(f"[bold bright_black]🌐 [当前session id] - {session_id} // 输入 '/exit' 退出cli[/bold bright_black]")

    # 装载全新的纯中文赛博调色样式与动态自动联想补全
    session = PromptSession(
        history=InMemoryHistory(),
        completer=CyberCommandCompleter(),
        style=cyber_tui_style,
        complete_while_typing=True
    )

    # 💎 【简约大气重构】去除所有喧宾夺主的字眼，恢复干净、现代且不越界的亮青色纯净提示符
    prompt_message = HTML("<ansicyan><b> ＞</b></ansicyan>")

    while True:
        try:
            sys.stdout.flush()

            # 异步非阻塞等待打字输入
            user_input = await session.prompt_async(prompt_message)
            user_input = user_input.strip()

        except (EOFError, KeyboardInterrupt):
            Console().print("\n[bold #ff0055]🚨 退出当前cli。[/bold #ff0055]")
            break

        if not user_input:
            continue

        # ========================================================
        # 🚀 斜杠指令硬件拦截：瞬间阻断大模型，由本地进行高级汉化回显
        # ========================================================
        if user_input.startswith("/"):
            should_exit = await handle_slash_command(user_input, session_id)
            if should_exit:
                break
            typer.echo("")
            continue

        # 投递真实量子流式交互引擎
        await run_loop_async(user_input, session_id, stream=True)

        # 战术空行隔离
        typer.echo("")


if __name__ == "__main__":
    app()