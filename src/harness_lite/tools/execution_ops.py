import os
import subprocess
import tempfile
import threading
import queue
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Tuple

from harness_lite.tools.base import BaseTool
from harness_lite.security.manager import security_manager

class PersistentShell:
    """
    底层持久化 Shell，保持持续的上下文 (CWD, ENV) 状态，防止发生进程死锁
    """

    def __init__(self, cwd: str):
        # 启动一个常驻的 bash 进程，合并 stdout 和 stderr
        self.process = subprocess.Popen(
            ['bash'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1
        )
        self.output_queue = queue.Queue()
        self.thread = threading.Thread(target=self._read_output, daemon=True)
        self.thread.start()

    def _read_output(self):
        for line in iter(self.process.stdout.readline, ''):
            self.output_queue.put(line)

    def execute(self, command: str, timeout: int = 15) -> Tuple[str, int]:
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                break

        # 注入唯一结束符与退出码，以便我们知道命令何时执行完毕
        marker = f"__EOC_{uuid.uuid4().hex}__"
        full_cmd = f"{command}\necho {marker} $?\n"

        self.process.stdin.write(full_cmd)
        self.process.stdin.flush()

        output_lines = []
        exit_code = 0
        start_time = time.time()

        while True:
            if time.time() - start_time > timeout:
                self.process.send_signal(subprocess.signal.SIGINT)
                return "".join(output_lines) + f"\n\n[Timeout] 执行超过 {timeout} 秒被强制中断。", -1

            try:
                line = self.output_queue.get(timeout=0.1)
                if marker in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            exit_code = int(parts[-1])
                        except ValueError:
                            pass
                    break
                output_lines.append(line)
            except queue.Empty:
                continue
        return "".join(output_lines), exit_code

class BashTerminalTool(BaseTool):
    """
    有记忆的持久化终端执行器
    """

    @property
    def name(self) -> str: return "bash_terminal"

    @property
    def description(self) -> str:
        return "执行标准的终端 Shell 命令。自带环境状态记忆，export 变量和 cd 目录切换会持续生效。请优先使用此工具运行代码、查看系统或安装依赖。"

    def __init__(self, timeout: int = 20):
        super().__init__()
        self.timeout = timeout
        self.shell = PersistentShell(cwd=str(security_manager.workspace_root))

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

        output, exit_code = self.shell.execute(command, timeout=self.timeout)

        # 截断超长输出，保护上下文
        if len(output) > 2500:
            output = output[:2500] + "\n...[输出过长，已被截断]..."

        if exit_code == 0:
            return f"Success (Exit 0):\n{output.strip()}" if output.strip() else "Success: 命令执行完毕，无输出。"
        elif exit_code == -1:
            return f"Failed: 命令执行超时。\n{output.strip()}"
        else:
            return f"Failed (Exit {exit_code}):\n{output.strip()}"

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

