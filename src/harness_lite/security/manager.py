from typing import Any, Dict, Optional
from datetime import datetime


class SecurityManager:
    """安全管理器，工具执行前的安全拦截"""

    def __init__(self):
        self._audit_log: list = []

    def check_permission(self, tool_name: str, user_id: str = "default") -> bool:
        """
        检查用户是否有权限调用指定工具

        Args:
            tool_name: 工具名称
            user_id: 用户 ID，默认 "default"

        Returns:
            是否允许执行
        """
        # 默认全部放行
        return True

    def validate_input(self, tool_name: str, input_data: Dict[str, Any]) -> bool:
        """
        校验工具输入数据

        Args:
            tool_name: 工具名称
            input_data: 输入数据字典

        Returns:
            输入是否合法
        """
        # 默认全部合法
        return True

    def audit_log(self, action: str, tool_name: str, user_id: str, result: str) -> None:
        """
        记录审计日志

        Args:
            action: 操作类型（如 "execute", "deny"）
            tool_name: 工具名称
            user_id: 用户 ID
            result: 操作结果
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "tool_name": tool_name,
            "user_id": user_id,
            "result": result
        }
        self._audit_log.append(log_entry)

    def get_audit_log(self, limit: int = 100) -> list:
        """
        获取审计日志

        Args:
            limit: 返回最近 N 条记录

        Returns:
            审计日志列表
        """
        return self._audit_log[-limit:]

    def clear_audit_log(self) -> None:
        """清空审计日志"""
        self._audit_log.clear()

    def intercept(self, tool_name: str, input_data: Dict[str, Any], user_id: str = "default") -> tuple:
        """
        执行完整的安全拦截流程

        Args:
            tool_name: 工具名称
            input_data: 输入数据
            user_id: 用户 ID

        Returns:
            (是否允许, 错误信息或 None)
        """
        # 1. 权限检查
        if not self.check_permission(tool_name, user_id):
            self.audit_log("deny", tool_name, user_id, "permission denied")
            return False, "Permission denied"

        # 2. 输入校验
        if not self.validate_input(tool_name, input_data):
            self.audit_log("deny", tool_name, user_id, "invalid input")
            return False, "Invalid input"

        # 3. 放行
        self.audit_log("allow", tool_name, user_id, "passed")
        return True, None


# 全局单例
security_manager = SecurityManager()
