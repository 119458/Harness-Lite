import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any
from harness_lite.tools.base import BaseTool

class BashTerminalTool(BaseTool):
    """
    受控的终端 Shell 执行器
    """
    @property
    def name(self) -> str:
        return "bash_terminal"
    @property
    def description(self) -> str:
        return "执行标准的终端 Shell 命令。自带目录状态记忆（支持 cd 命令）。适用于运行测试、查看系统状态或安装依赖。注意：长耗时命令会被自动强制中断。"

    def __init__(self, timeout: int = 15):
        super().__init__()
        self.timeout = timeout
        # 初始化状态：记录当前工作目录，确保多次调用工具时上下文连贯
        self.current_working_dir = os.getcwd()
        # 作为最后一道防线的内置黑名单（更复杂的鉴权应在 SecurityManager 中进行）
        self.blacklist = ['rm -rf /', 'mkfs', 'sudo']

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令，例如 'ls -la', 'python test.py', 'cd src' 等"
            }
        }
        schema["function"]["parameters"]["required"] = ["command"]
        return schema

    def execute(self, command: str) -> str:
        command = command.strip()
        if not command:
            return "Error: 接收到了空命令。"

        for blocked in self.blacklist:
            if blocked in command:
                return f"Security Error: 命令触碰了底层安全策略，包含高危指令 '{blocked}' 被拦截。"

        if command.startswith("cd") or command == "cd":
            target_dir = command[3:].strip()
            if not target_dir or target_dir == "~":
                target_dir = os.path.expanduser("~")

            new_path = Path(self.current_working_dir) / target_dir
            resolved_path = new_path.resolve()

            if resolved_path.exists() and resolved_path.is_dir():
                self.current_working_dir = str(resolved_path)
                return f"Success: 已将当前工作目录切换至 {self.current_working_dir}"
            else:
                return f"Error: 目录 '{target_dir}' 不存在。"

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.current_working_dir,
                capture_output=True,
                timeout=self.timeout
            )
            output_parts = []
            if result.stdout:
                stdout_text = result.stdout[:2000] + "\n...[输出被截断]" if len(result.stdout) > 2000 else result.stdout
                output_parts.append(f"STDOUT:\n{stdout_text.strip()}")
            if result.stderr:
                stderr_text = result.stderr[:2000] + "\n...[输出被截断]" if len(result.stderr) > 2000 else result.stderr
                output_parts.append(f"STDERR:\n{stderr_text.strip()}")
            combined_output = "\n".join(output_parts)

            if result.returncode == 0:
                return f"Success (Exit code: 0):\n{combined_output}" if combined_output else "Success: 命令执行完毕，无输出。"
            else:
                return f"Failed (Exit code: {result.returncode}):\n{combined_output}"

        except subprocess.TimeoutExpired:
            return (f"Error: 命令执行超过了最大时限 ({self.timeout}秒) 被强制中断。\n"
                    f"如果是启动服务器类型的命令（如 npm run dev），请将其作为后台任务运行或只将其用于测试。")
        except Exception as e:
            return f"System Error: {str(e)}"

class PythonInterpreterTool(BaseTool):
    """
    独立的 Python 交互式沙盒
    """
    @property
    def name(self) -> str:
        return "python_interpreter"
    @property
    def description(self) -> str:
        return "Python 代码沙盒环境。允许编写并执行一小段 Python 脚本，并获取其打印的输出(print)或报错堆栈。常用于计算、数据清洗或验证逻辑。"

    def __init__(self, timeout: int = 10):
        super().__init__()
        self.timeout = timeout
        self.current_working_dir = os.getcwd()

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "code": {
                "type": "string",
                "description": "要执行的 Python 脚本代码。注意：必须通过 print() 函数打印结果才能被外界看到。"
            }
        }
        schema["function"]["parameters"]["required"] = ["code"]
        return schema

    def execute(self, code: str) -> str:
        if not code.strip():
            return "Error: 代码为空。"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(code)
            temp_file_path = temp_file.name

        try:
            result = subprocess.run(
                ['python', temp_file_path],
                cwd=self.current_working_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout.strip()}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr.strip()}\n"

            if result.returncode == 0:
                return f"Execution Success:\n{output}".strip() if output else "Success: 代码运行完毕，但没有任何打印输出。"
            else:
                return f"Execution Failed (Exit code {result.returncode}):\n{output}".strip()

        except subprocess.TimeoutExpired:
            return f"Error: Python 代码执行超时 ({self.timeout}秒)。可能存在死循环或长时间的网络请求。"
        except Exception as e:
            return f"System Error: {str(e)}"

        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

