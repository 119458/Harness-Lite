"""Security module tests."""
import pytest
from harness_lite.security import SecurityManager


class TestSecurityManager:
    """Security manager tests."""

    def test_default_allows(self):
        """Verify default permission check allows all."""
        manager = SecurityManager()
        assert manager.check_permission("any_tool", "any_user") is True

    def test_validate_input_default(self):
        """Verify default input validation passes all."""
        manager = SecurityManager()
        assert manager.validate_input("tool", {"key": "value"}) is True

    def test_audit_log_records(self):
        """Verify audit log records entries correctly."""
        manager = SecurityManager()
        manager.audit_log("test_action", "test_tool", "test_user", "success")
        logs = manager.get_audit_log()
        assert len(logs) == 1
        assert logs[0]["action"] == "test_action"
        assert logs[0]["tool_name"] == "test_tool"
        assert logs[0]["user_id"] == "test_user"
        assert logs[0]["result"] == "success"

    def test_audit_log_has_timestamp(self):
        """Verify audit log entries have timestamps."""
        manager = SecurityManager()
        manager.audit_log("test_action", "test_tool", "test_user", "success")
        logs = manager.get_audit_log()
        assert "timestamp" in logs[0]

    def test_intercept_allows_by_default(self):
        """Verify intercept default behavior allows execution."""
        manager = SecurityManager()
        allowed, error = manager.intercept("tool", {"data": "value"})
        assert allowed is True
        assert error is None

    def test_clear_audit_log(self):
        """Verify clearing audit log removes all entries."""
        manager = SecurityManager()
        manager.audit_log("test", "tool", "user", "result")
        manager.clear_audit_log()
        assert len(manager.get_audit_log()) == 0

    def test_get_audit_log_with_limit(self):
        """Verify getting audit log with limit works."""
        manager = SecurityManager()
        for i in range(150):
            manager.audit_log(f"action_{i}", "tool", "user", "result")

        logs = manager.get_audit_log(limit=100)
        assert len(logs) == 100

    def test_get_audit_log_default_limit(self):
        """Verify getting audit log uses default limit."""
        manager = SecurityManager()
        for i in range(150):
            manager.audit_log(f"action_{i}", "tool", "user", "result")

        logs = manager.get_audit_log()
        assert len(logs) == 100  # default limit

    def test_multiple_audit_logs(self):
        """Verify multiple audit logs are recorded in order."""
        manager = SecurityManager()
        manager.audit_log("action1", "tool1", "user1", "result1")
        manager.audit_log("action2", "tool2", "user2", "result2")
        manager.audit_log("action3", "tool3", "user3", "result3")

        logs = manager.get_audit_log()
        assert len(logs) == 3
        assert logs[0]["action"] == "action1"
        assert logs[1]["action"] == "action2"
        assert logs[2]["action"] == "action3"

    def test_intercept_logs_allow_action(self):
        """Verify intercept logs 'allow' action when passing."""
        manager = SecurityManager()
        allowed, _ = manager.intercept("tool", {"data": "value"})

        logs = manager.get_audit_log()
        assert len(logs) == 1
        assert logs[0]["action"] == "allow"
        assert logs[0]["tool_name"] == "tool"

    def test_audit_log_entry_structure(self):
        """Verify audit log entry has all required fields."""
        manager = SecurityManager()
        manager.audit_log("test_action", "test_tool", "test_user", "test_result")
        logs = manager.get_audit_log()
        entry = logs[0]

        assert "timestamp" in entry
        assert "action" in entry
        assert "tool_name" in entry
        assert "user_id" in entry
        assert "result" in entry
    def test_long_term_memory_relative_path_resolves_from_project_root(self):
        """memory_store/long_term 相对路径必须按项目根解析，不能按长期记忆沙箱重复拼接。"""
        manager = SecurityManager()
        target = manager.project_root / "memory_store" / "long_term" / "frontend-pipeline-analogy.md"
        allowed, resolved = manager._check_path_jail(
            "memory_store/long_term/frontend-pipeline-analogy.md",
            "test-session",
        )
        assert allowed is True
        assert resolved == str(target.resolve())
        assert "memory_store/long_term/memory_store/long_term" not in resolved

    def test_long_term_memory_relative_path_matching_is_case_insensitive(self):
        """大小写不敏感文件系统上，非规范大小写也不应回退到 session workspace 重复拼接。"""
        manager = SecurityManager()
        allowed, resolved = manager._check_path_jail(
            "Memory_Store/Long_Term/frontend-pipeline-analogy.md",
            "test-session",
        )
        assert allowed is True
        assert "memory_store/long_term/memory_store/long_term" not in resolved.lower()


class TestGlobalSecurityManager:
    """Global security manager tests."""

    def test_global_security_manager_exists(self):
        """Verify global security manager singleton exists."""
        from harness_lite.security import security_manager

        assert security_manager is not None
        assert isinstance(security_manager, SecurityManager)

    def test_global_security_manager_functional(self):
        """Verify global security manager is functional."""
        from harness_lite.security import security_manager

        allowed, error = security_manager.intercept("tool", {})
        assert allowed is True
        assert error is None

        security_manager.audit_log("test", "tool", "user", "result")
        assert len(security_manager.get_audit_log()) >= 1
