import os
import sys
import subprocess
import tempfile
import threading
import queue
import time
import uuid
import logging
import contextvars
from email.policy import default
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

try:
    import resource
except ImportError:
    resource = None

from harness_lite.tools.base import BaseTool
from harness_lite.security.manager import security_manager

logger = logging.getLogger("harness_lite.execution")

current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_session_id", default="default")

class IsolatedPersistentShell:
    """
    会话独占型、带资源硬限制与故障自愈能力的持久化 Shell 驱动器。
    """

    def __init__(self, session_id: str, initial_cwd: str):
        self.session_id = session_id
        self.initial_cwd = initial_cwd

        self.last_known_cwd = initial_cwd
        self.env_snapshots: Dict[str, str] = {}

        self.process: Optional[subprocess.Popen] = None
        self.output_queue: queue.Queue = queue.Queue()
        self.reader_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        self._start_process()

    def _set_resource_limits(self):
        """
        在子进程 fork 出来之后，真正 exec 执行 bash 之前，注入物理资源配额硬限制
        """
        if not resource:
            return
        try:
            # 1. 限制子进程最大虚拟内存配额（例如最大 1GB），防止大模型恶意/误写死循环脚本撑爆宿主机 RAM
            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
            # 2. 限制子进程可以派生的最大进程数（例如最多 50 个），阻断 Fork 炸弹攻击
            resource.setrlimit(resource.RLIMIT_NPROC, (50, 50))
            # 3. 限制单个文件最大写入尺寸（例如最大 200MB），防止恶意填满宿主机磁盘
            resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
        except Exception as e:
            # 静默降级，避免由于权限或平台差异导致主循环崩溃
            pass

    def _start_process(self):
        """
        拉起一个干净、受控、开启独立进程组的常驻 bash 进程
        """
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                break

        self.process = subprocess.Popen(
            ['bash'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.last_known_cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            preexec_fn=self._set_resource_limits if os.name != 'nt' else None
        )

        self.reader_thread = threading.Thread(target=self._read_output_loop, daemon=True)
        self.reader_thread.start()

    def _read_output_loop(self):
        """
        流式读取标准输出的常驻工作线程
        """
        try:
            for line in iter(self.process.stdout.readline, ''):
                self.output_queue.put(line)
        except Exception:
            pass

    def _heal_and_revert(self):
        """
        【核心自愈逻辑】强杀故障进程，重新拉起新环境，并自动重放路径状态
        """
        logger.warning(f"[Session-{self.session_id}] 进程触发故障自愈机制。正在强杀并重组环境...")
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), subprocess.signal.SIGTERM)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

        self._start_process()

        marker = f"__HEAL_SYNC_{uuid.uuid4().hex}__"
        heal_cmd = f"cd '{self.last_known_cwd}' && echo '{marker}'\n"

        try:
            self.process.stdin.write(heal_cmd)
            self.process.stdin.flush()

            start_t = time.time()
            while time.time() - start_t < 2:
                try:
                    line = self.output_queue.get(timeout=0.05)
                    if marker in line:
                        break
                except queue.Empty:
                    continue
        except Exception as e:
            logger.error(f"自愈状态重放失败: {str(e)}")

    def execute(self, command: str, timeout: int = 15) -> Tuple[str, int]:
        """
        带安全防护与超时侦测的排他性命令执行管线
        """
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self._start_process()

            while not self.output_queue.empty():
                try:
                    self.output_queue.get_nowait()
                except queue.Empty:
                    break

            # 构造唯一结束标记符与状态退出码锚点
            marker = f"__EOC_{uuid.uuid4().hex}__"
            # 每次命令执行后，通过 pwd 强行刺探当前真实的物理工作目录，用于故障自愈重放和沙箱逃逸越界审查
            full_cmd = f"{command}\necho '{marker}' $? \"$(pwd)\"\n"

            try:
                self.process.stdin.write(full_cmd)
                self.process.stdin.flush()
            except (IOError, BrokenPipeError) as e:
                self._heal_and_revert()
                try:
                    self.process.stdin.write(full_cmd)
                    self.process.stdin.flush()
                except Exception as e:
                    return f"Failed: 管道彻底损坏，自愈重试失败。详情: {str(e)}", -2

            output_lines = []
            exit_code = 0
            start_time = time.time()
            triggered_timeout = False

            while True:
                if time.time() - start_time > timeout:
                    triggered_timeout = True
                    break
                try:
                    line = self.output_queue.get(timeout=0.1)
                    if marker in line:
                        parts = line.strip().split()
                        if len(parts) >= 3:
                            try:
                                exit_code = int(parts[-2])
                                reported_cwd = parts[-1]
                                if not security_manager.is_path_safe(Path(reported_cwd)):
                                    self._heal_and_revert()
                                    return f"[Security Escape Blocked] 警告: 检测到当前命令试图将工作目录切换至沙箱外部 '{reported_cwd}'。此操作已被拦截，环境已强制回滚复位。", -3
                                self.last_known_cwd = reported_cwd
                            except ValueError:
                                pass
                        break
                    output_lines.append(line)
                except queue.Empty:
                    continue
            if triggered_timeout:
                self._heal_and_revert()
                return "".join(output_lines) + f"\n\n[Timeout] 命令执行超过 {timeout} 秒未闭合，已触发自愈系统强杀通道并回滚环境。", -1

            return "".join(output_lines), exit_code


class SessionProcessManager:
    """
    线程安全的多租户 Worker 进程池管理器。
    负责根据全局不同的 session_id 分发、托管及隔离专有的 Shell 实例。
    """
    def __init__(self):
        self._pools: Dict[str, IsolatedPersistentShell] = {}
        self._lock = threading.Lock()

    def get_shell(self, session_id: str) -> IsolatedPersistentShell:
        """
        获取或动态分配专属 Session 的多租户隔离沙盒进程
        """
        with self._lock:
            if session_id not in self._pools:
                session_root_dir = str(security_manager.get_session_workspace(session_id))
                self._pools[session_id] = IsolatedPersistentShell(session_id, session_root_dir)
            return self._pools[session_id]

# 全局多租户 Worker 进程池单例
process_manager = SessionProcessManager()


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


class PythonInterpreterTool(BaseTool):
    """
    多租户资源物理配额受控的 Python 交互式沙盒
    """

    @property
    def name(self) -> str:
        return "python_interpreter"

    @property
    def description(self) -> str:
        return "Python 代码沙盒环境。允许编写并执行一小段 Python 脚本（支持导入 os/sys 进行无害的路径及环境处理），并通过 print() 打印结果。"

    def __init__(self, timeout: int = 10):
        super().__init__()
        self.timeout = timeout

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

    def _set_python_limits(self):
        """为临时的 Python 解释器注入独立的资源硬配额"""
        if not resource: return
        try:
            # 限制交互式脚本的最大内存，防止恶意内存泄露死循环
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            # 限制最大 CPU 物理执行秒数
            resource.setrlimit(resource.RLIMIT_CPU, (self.timeout + 1, self.timeout + 1))
        except Exception:
            pass

    def execute(self, code: str) -> str:
        if not code.strip():
            return "Error: 代码为空。"

        session_id = current_session_id.get()
        # 强制将临时脚本的运行和生成锁定在当前租户专属的沙箱子文件夹内
        session_root = security_manager.get_session_workspace(session_id)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8",
                                         dir=session_root) as temp_file:
            temp_file.write(code)
            temp_file_path = temp_file.name

        try:
            result = subprocess.run(
                ['python', temp_file_path],
                cwd=str(session_root),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                preexec_fn=self._set_python_limits if os.name != 'nt' else None
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
            return f"Error: Python 代码执行超时 ({self.timeout}秒)。物理资源限制引擎已强制熔断该进程。可能存在死循环或长时间的网络请求。"
        except Exception as e:
            return f"System Error: {str(e)}"

        finally:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except Exception:
                    pass