"""阶段 D 测试 - task_scheduler 工具与持久化层

覆盖：
- create / list / delete / pause / resume 五种 action 行为
- TaskRepository JSON 持久化（写后读、update、remove）
- 安全层：interval < 60、cron 表达式非法、action 非法
- 不启动真实 TaskDispatcher 后台线程（mock 掉）
- 同名重复 create：当前实现允许同名（仅 id 唯一），用 xfail 标注现状
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import harness_lite.tools  # noqa: F401
from harness_lite.tools import TaskSchedulerTool
from harness_lite.tools.task_scheduler.task_store import TaskRepository
from harness_lite.security.manager import SecurityManager


@pytest.fixture(autouse=True)
def _mock_dispatcher(tmp_path, monkeypatch):
    """关键：避免真实启动调度后台线程，并把 store 重定向到 tmp_path"""
    monkeypatch.setattr(
        TaskSchedulerTool,
        "_resolve_store_path",
        staticmethod(lambda: tmp_path / "tasks.json"),
    )
    # 把 TaskDispatcher.get_or_create 替换为 noop，避免起线程
    noop_dispatcher = MagicMock()
    noop_dispatcher.start = MagicMock()
    with patch(
        "harness_lite.tools.task_scheduler.scheduler_tool.TaskDispatcher.get_or_create",
        return_value=noop_dispatcher,
    ):
        yield


@pytest.fixture
def tool():
    return TaskSchedulerTool()


@pytest.fixture
def isolated_security(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return SecurityManager()


# ============================================================
# 工具层 action 行为
# ============================================================

def test_create_interval_task_returns_id(tool):
    result = tool.execute(
        action="create",
        name="健康检查",
        schedule_type="interval",
        schedule_value="120",
        prompt="ping 一下",
    )
    assert result.startswith("Success"), result
    # 返回中要带 task_id（8 字符 hex）
    assert "已创建定时任务" in result


def test_list_after_create(tool):
    tool.execute(action="create", name="任务A", schedule_type="interval",
                 schedule_value="120", prompt="A")
    tool.execute(action="create", name="任务B", schedule_type="interval",
                 schedule_value="180", prompt="B")
    result = tool.execute(action="list")
    assert "任务A" in result and "任务B" in result
    assert "共 2 个任务" in result


def test_list_empty(tool):
    result = tool.execute(action="list")
    assert "没有任何定时任务" in result


def test_delete_then_list(tool):
    create_msg = tool.execute(action="create", name="临时", schedule_type="interval",
                              schedule_value="60", prompt="X")
    # 提取 task_id
    # Success: 已创建定时任务 <id>\n ...
    task_id = create_msg.split("已创建定时任务")[1].split("\n")[0].strip()

    del_result = tool.execute(action="delete", task_id=task_id)
    assert del_result.startswith("Success")

    listed = tool.execute(action="list")
    assert task_id not in listed


def test_delete_unknown_task(tool):
    result = tool.execute(action="delete", task_id="ffffffff")
    assert result.startswith("Error")
    assert "未找到" in result


def test_pause_resume_lifecycle(tool):
    create_msg = tool.execute(action="create", name="P", schedule_type="interval",
                              schedule_value="60", prompt="X")
    task_id = create_msg.split("已创建定时任务")[1].split("\n")[0].strip()

    paused = tool.execute(action="pause", task_id=task_id)
    assert paused.startswith("Success") and "暂停" in paused
    listed_paused = tool.execute(action="list")
    assert "已暂停" in listed_paused

    resumed = tool.execute(action="resume", task_id=task_id)
    assert resumed.startswith("Success") and "恢复" in resumed
    listed_resumed = tool.execute(action="list")
    assert "启用" in listed_resumed


def test_create_missing_fields(tool):
    result = tool.execute(action="create")
    assert result.startswith("Error")
    assert "缺少必填参数" in result


def test_unknown_action(tool):
    result = tool.execute(action="explode")
    assert result.startswith("Error")


# ============================================================
# 重复同名 create —— 当前实现允许同名，仅 id 唯一
# ============================================================

@pytest.mark.xfail(
    reason="当前实现允许同名任务（仅 id 唯一）；如未来产品要求按 name 去重，请改为 strict pass。",
    strict=False,
)
def test_create_same_name_should_be_rejected(tool):
    tool.execute(action="create", name="dup", schedule_type="interval",
                 schedule_value="60", prompt="X")
    second = tool.execute(action="create", name="dup", schedule_type="interval",
                          schedule_value="60", prompt="X")
    assert second.startswith("Error"), "如果业务上不允许同名任务，这里应拒绝"


# ============================================================
# 任务上限：当前实现 20 个上限是工具层的；接近上限测试代价大，标 xfail
# ============================================================

def test_active_task_limit_enforced(tool):
    """创建到 20 个时，再创建必须报错"""
    for i in range(20):
        msg = tool.execute(action="create", name=f"t{i}", schedule_type="interval",
                           schedule_value="60", prompt=f"do{i}")
        assert msg.startswith("Success"), msg
    overflow = tool.execute(action="create", name="t20", schedule_type="interval",
                            schedule_value="60", prompt="overflow")
    assert overflow.startswith("Error")
    assert "20" in overflow


# ============================================================
# TaskRepository 持久化层
# ============================================================

def test_repository_add_and_load(tmp_path):
    repo = TaskRepository(tmp_path / "store.json")
    task = {
        "id": "abc12345",
        "name": "持久化测试",
        "schedule_type": "interval",
        "schedule_value": "120",
        "prompt": "hi",
        "enabled": True,
    }
    repo.add(task)
    all_tasks = repo.load_all()
    assert len(all_tasks) == 1
    assert all_tasks[0]["id"] == "abc12345"
    # 文件确实落盘
    assert (tmp_path / "store.json").exists()


def test_repository_update_merges(tmp_path):
    repo = TaskRepository(tmp_path / "store.json")
    repo.add({"id": "x1", "name": "n1", "schedule_type": "interval",
              "schedule_value": "60", "prompt": "p", "enabled": True})
    ok = repo.update("x1", {"enabled": False})
    assert ok is True
    assert repo.get("x1")["enabled"] is False
    # 其它字段保留
    assert repo.get("x1")["name"] == "n1"


def test_repository_update_missing_returns_false(tmp_path):
    repo = TaskRepository(tmp_path / "store.json")
    assert repo.update("nope", {"enabled": False}) is False


def test_repository_remove(tmp_path):
    repo = TaskRepository(tmp_path / "store.json")
    repo.add({"id": "x1", "name": "n1", "schedule_type": "interval",
              "schedule_value": "60", "prompt": "p", "enabled": True})
    assert repo.remove("x1") is True
    assert repo.load_all() == []
    assert repo.remove("x1") is False


def test_repository_duplicate_id_raises(tmp_path):
    repo = TaskRepository(tmp_path / "store.json")
    repo.add({"id": "x1", "name": "n1", "schedule_type": "interval",
              "schedule_value": "60", "prompt": "p", "enabled": True})
    with pytest.raises(ValueError):
        repo.add({"id": "x1", "name": "n2", "schedule_type": "interval",
                  "schedule_value": "60", "prompt": "p", "enabled": True})


# ============================================================
# 安全层校验
# ============================================================

def test_security_blocks_interval_below_60(isolated_security):
    allowed, msg = isolated_security.intercept("task_scheduler", {
        "action": "create",
        "name": "spam",
        "schedule_type": "interval",
        "schedule_value": "5",
        "prompt": "x",
    })
    assert allowed is False
    assert "60" in (msg or "")


def test_security_blocks_invalid_action(isolated_security):
    allowed, msg = isolated_security.intercept("task_scheduler", {
        "action": "drop_all",
    })
    assert allowed is False
    assert "action" in (msg or "")


def test_security_blocks_unknown_schedule_type(isolated_security):
    allowed, msg = isolated_security.intercept("task_scheduler", {
        "action": "create",
        "name": "n",
        "schedule_type": "yearly",
        "schedule_value": "x",
        "prompt": "p",
    })
    assert allowed is False
    assert "schedule_type" in (msg or "")


def test_security_blocks_invalid_cron_when_croniter_available(isolated_security):
    """若已安装 croniter，应通过其语法校验拦截非法表达式；缺包时跳过"""
    try:
        import croniter  # noqa: F401
    except ImportError:
        pytest.skip("croniter 未安装，跳过 cron 语法校验测试")

    allowed, msg = isolated_security.intercept("task_scheduler", {
        "action": "create",
        "name": "n",
        "schedule_type": "cron",
        "schedule_value": "not a real cron",
        "prompt": "p",
    })
    assert allowed is False
    assert "cron" in (msg or "")


def test_security_allows_valid_cron_when_croniter_available(isolated_security):
    try:
        import croniter  # noqa: F401
    except ImportError:
        pytest.skip("croniter 未安装")

    allowed, _ = isolated_security.intercept("task_scheduler", {
        "action": "create",
        "name": "n",
        "schedule_type": "cron",
        "schedule_value": "*/5 * * * *",
        "prompt": "p",
    })
    assert allowed is True


def test_security_allows_list_action(isolated_security):
    allowed, _ = isolated_security.intercept("task_scheduler", {"action": "list"})
    assert allowed is True
