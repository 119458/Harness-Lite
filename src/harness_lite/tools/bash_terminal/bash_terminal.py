"""会话独占型持久化 Bash 终端工具"""

from typing import Dict, Any

from harness_lite.tools.base import BaseTool
from .process_manager import current_session_id, process_manager


class BashTerminalTool(BaseTool):
    """
    多租户有记忆状态隔离的持久化终端执行器
    """

    @property
    def name(self) -> str:
        return "bash_terminal"

    @property
    def description(self) -> str:
        return "执行标准的终端 Shell 命令。自带租户环境状态隔离记忆，cd 目录切换会持续生效。请优先使用此工具运行代码、查看系统或安装依赖。"

    def __init__(self, timeout: int = 20):
        super().__init__()
        self.timeout = timeout

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令"
            }
        }
        schema["function"]["parameters"]["required"] = ["command"]
        return schema

    def execute(self, command: str) -> str:
        command = command.strip()
        if not command:
            return "Error: 接收到了空命令。"

        # 动态从 ContextVar 上下文中提取当前的专属会话 ID
        session_id = current_session_id.get()
        shell = process_manager.get_shell(session_id)

        output, exit_code = shell.execute(command, timeout=self.timeout)

        # 保护大模型上下文，防止冗长日志撑爆窗口
        if len(output) > 2500:
            output = output[:2500] + "\n...[输出过长，已被系统安全截断]..."

        if exit_code == 0:
            return f"Success (Exit 0):\n{output.strip()}" if output.strip() else "Success: 命令执行完毕，无输出。"
        elif exit_code == -1:
            return f"Failed: 命令执行超时并导致环境重置自愈。\n{output.strip()}"
        elif exit_code == -3:
            return output.strip()  # 返回沙箱路径越界逃逸拦截日志
        else:
            return f"Failed (Exit {exit_code}):\n{output.strip()}"
