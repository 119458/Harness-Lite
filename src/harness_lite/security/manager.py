import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from datetime import datetime


class SecurityManager:
    """安全管理器：负责全局的安全拦截、沙箱隔离与路径智能重写"""

    def __init__(self):
        self._audit_log: list = []

        # 1. 动态计算项目根目录 (Harness-Lite/) 并锁定 sandbox 文件夹
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent.parent

        # 优先读取环境变量中的自定义路径，否则默认锁定在项目根目录下的 sandbox/ 文件夹
        workspace_env = os.environ.get("WORKSPACE_ROOT")
        if workspace_env:
            self.workspace_root = Path(workspace_env).resolve()
        else:
            self.workspace_root = (project_root / "sandbox").resolve()

        # 自动创建 sandbox 文件夹
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        # 2. 定义高危 Shell 指令特征库
        self.dangerous_patterns = [
            r"\bsudo\b",
            r"\brm\b\s+-(?:[rRfF]+|.*\s+/[^\s]*)",
            r"\bmkfs\b",
            r"\bchmod\b\s+[70]77",
            r"\bchown\b",
            r">\s*/dev/(?:sd[a-z]|nvme)",
            r"\bcurl\b.*\|\s*(?:bash|sh)",
            r"\bwget\b.*\|\s*(?:bash|sh)",
            r"\bmv\b\s+.*\s+/(?:etc|usr|bin|lib|var)",
        ]

    def _check_workspace_jail(self, target_path: str) -> Tuple[bool, str]:
        """沙箱拦截核心逻辑：计算并返回安全的绝对路径"""
        try:
            p = Path(target_path)
            # 【核心修复点】必须在调用 resolve() 之前判断大模型传入的是否为相对路径
            if not p.is_absolute():
                # 如果是相对路径，强制锚定到沙箱根目录进行拼接，再解析！
                resolved_path = (self.workspace_root / p).resolve()
            else:
                resolved_path = p.resolve()

            # 严格判断解析后的绝对路径是否在 sandbox 文件夹内
            if not resolved_path.is_relative_to(self.workspace_root):
                return False, f"Sandbox Violations: 尝试访问沙箱外部路径 '{resolved_path}'。你已被安全限制在 '{self.workspace_root}' 目录内。"

            # 安全通过后，返回我们计算好的真正的安全绝对路径
            return True, str(resolved_path)
        except Exception as e:
            return False, f"Path Resolution Error: 路径解析异常 ({str(e)})"

    def _check_dangerous_command(self, command: str) -> Tuple[bool, str]:
        """高危 Shell 指令拦截"""
        for pattern in self.dangerous_patterns:
            if re.search(pattern, command):
                return False, f"High-Risk Command Blocked: 核心策略拦截。不允许执行改变宿主机关键状态的指令: '{command}'。"
        return True, ""

    def validate_input(self, tool_name: str, input_data: Dict[str, Any]) -> Tuple[bool, str]:
        """根据工具特性，进行针对性的输入校验、拦截与【路径重写】"""

        # 1. 拦截所有文件读写工具，并进行“智能路径重写”
        if tool_name in ["read_file", "create_file", "edit_file"]:
            path_arg = input_data.get("file_path")
            if path_arg:
                safe, result_msg = self._check_workspace_jail(path_arg)
                if not safe:
                    return False, result_msg
                # 【核心修复点】既然算好了沙箱里的绝对路径，就直接强行覆盖原参数！
                # 这样工具层拿到的一定是位于 sandbox 内的绝对路径
                input_data["file_path"] = result_msg

        # 目录查看工具同理
        elif tool_name == "list_directory":
            path_arg = input_data.get("path", ".")
            safe, result_msg = self._check_workspace_jail(path_arg)
            if not safe:
                return False, result_msg
            input_data["path"] = result_msg

        # 2. 拦截终端高危命令
        elif tool_name == "bash_terminal":
            cmd_arg = input_data.get("command", "")
            if cmd_arg:
                safe, error_msg = self._check_dangerous_command(cmd_arg)
                if not safe:
                    return False, error_msg

        return True, ""

    def check_permission(self, tool_name: str, user_id: str = "default") -> bool:
        return True

    def audit_log(self, action: str, tool_name: str, user_id: str, result: str) -> None:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "tool_name": tool_name,
            "user_id": user_id,
            "result": result
        }
        self._audit_log.append(log_entry)

    def intercept(self, tool_name: str, input_data: Dict[str, Any], user_id: str = "default") -> Tuple[
        bool, Optional[str]]:
        if not self.check_permission(tool_name, user_id):
            self.audit_log("deny", tool_name, user_id, "permission denied")
            return False, "Permission denied"

        # 这里的 validate_input 如果通过，会把 input_data 里面的路径重写为绝对路径
        is_valid, error_msg = self.validate_input(tool_name, input_data)
        if not is_valid:
            self.audit_log("deny", tool_name, user_id, f"blocked: {error_msg}")
            return False, error_msg

        self.audit_log("allow", tool_name, user_id, "passed")
        return True, None


# 全局单例
security_manager = SecurityManager()