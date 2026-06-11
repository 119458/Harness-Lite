"""
持久化 Shell 进程管理层

包含：
- ``current_session_id``：会话 ID 的 ContextVar（跨工具共享）
- ``IsolatedPersistentShell``：会话独占的 bash 子进程，带资源硬限制与故障自愈
- ``SessionProcessManager``：多租户线程安全的 shell 进程池
- ``process_manager``：模块级全局单例
"""

import os
import subprocess
import threading
import queue
import time
import uuid
import logging
import contextvars
from pathlib import Path
from typing import Dict, Tuple, Optional

try:
    import resource
except ImportError:
    resource = None

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
            preexec_fn=self._set_resource_limits if os.name != 'nt' else None,
            start_new_session=True
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
