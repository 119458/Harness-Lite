"""定时任务工具：对 LLM 暴露 create / list / delete / pause / resume 五种 action

约束：
- 单进程最多 20 个活动任务（防资源耗尽）。
- interval 最小 60 秒（防高频触发）。
- cron 表达式必须能被 croniter 校验；无 croniter 时仅允许 interval / once。
- 任务执行通道为本阶段先落到 pending_prompts.json，
  真正喂回 ReAct 引擎留待后续阶段。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from harness_lite.tools.base import BaseTool

from .scheduler_service import TaskDispatcher
from .task_store import TaskRepository

_MAX_ACTIVE_TASKS = 20
_MIN_INTERVAL_SECONDS = 60
_RELATIVE_TIME_PATTERN = re.compile(r"^\+(\d+)([smhd])$")
_VALID_ACTIONS = {"create", "list", "delete", "pause", "resume"}
_VALID_SCHEDULE_TYPES = {"cron", "interval", "once"}


class TaskSchedulerTool(BaseTool):
    """轻量定时任务工具"""

    def __init__(self) -> None:
        super().__init__()
        self._repository: Optional[TaskRepository] = None
        self._dispatcher: Optional[TaskDispatcher] = None

    @property
    def name(self) -> str:
        return "task_scheduler"

    @property
    def description(self) -> str:
        return (
            "管理定时任务。支持 cron 表达式、interval（固定秒数）、once（一次性时间点）"
            "三种调度方式。可执行 create（创建）、list（查询列表）、delete（删除）、"
            "pause（暂停）、resume（恢复）。任务到期后会把指定 prompt 投递到本地待办文件，"
            "供后续 ReAct 循环消费。注意 interval 最小 60 秒，单进程最多保留 20 个活动任务。"
        )

    def get_schema(self) -> Dict[str, Any]:
        schema = super().get_schema()
        schema["function"]["parameters"]["properties"] = {
            "action": {
                "type": "string",
                "enum": sorted(_VALID_ACTIONS),
                "description": "操作类型：create=创建，list=查看全部任务，delete=按 ID 删除，pause=暂停，resume=恢复。",
            },
            "task_id": {
                "type": "string",
                "description": "任务 ID。delete/pause/resume 必填，create/list 不需要。",
            },
            "name": {
                "type": "string",
                "description": "任务名称，仅 create 必填，用于人类可读地标识任务。",
            },
            "schedule_type": {
                "type": "string",
                "enum": sorted(_VALID_SCHEDULE_TYPES),
                "description": "调度类型：cron（cron 表达式）、interval（固定间隔秒数）、once（一次性触发）。",
            },
            "schedule_value": {
                "type": "string",
                "description": (
                    "调度具体值：cron 对应 5 段 cron 表达式；interval 对应正整数秒；"
                    "once 对应 ISO8601 时间或相对时间（+30s/+5m/+2h/+1d）。"
                ),
            },
            "prompt": {
                "type": "string",
                "description": "到期触发时要交付给 Agent 的指令文本（create 必填）。",
            },
        }
        schema["function"]["parameters"]["required"] = ["action"]
        return schema

    # ---------- 入口 ----------

    def execute(self,
                action: str,
                task_id: Optional[str] = None,
                name: Optional[str] = None,
                schedule_type: Optional[str] = None,
                schedule_value: Optional[str] = None,
                prompt: Optional[str] = None) -> str:
        if action not in _VALID_ACTIONS:
            return f"Error: 未知 action '{action}'，可选值为 {sorted(_VALID_ACTIONS)}。"

        try:
            self._ensure_dispatcher_ready()
        except Exception as exc:  # noqa: BLE001
            return f"Error: 调度器初始化失败（{exc}）。"

        handler_map = {
            "create": lambda: self._do_create(name, schedule_type, schedule_value, prompt),
            "list": self._do_list,
            "delete": lambda: self._do_delete(task_id),
            "pause": lambda: self._do_set_enabled(task_id, False),
            "resume": lambda: self._do_set_enabled(task_id, True),
        }
        return handler_map[action]()

    # ---------- 依赖装载 ----------

    def _ensure_dispatcher_ready(self) -> None:
        if self._dispatcher and self._repository:
            return
        store_path = self._resolve_store_path()
        self._repository = TaskRepository(store_path)
        self._dispatcher = TaskDispatcher.get_or_create(self._repository)
        self._dispatcher.start()

    @staticmethod
    def _resolve_store_path() -> Path:
        """优先放在项目 memory_store/scheduler/tasks.json"""
        # 项目根 = src/harness_lite/tools/task_scheduler/scheduler_tool.py 上溯 4 层
        project_root = Path(__file__).resolve().parents[4]
        return project_root / "memory_store" / "scheduler" / "tasks.json"

    # ---------- 各种 action 实现 ----------

    def _do_create(self, name: Optional[str], schedule_type: Optional[str],
                   schedule_value: Optional[str], prompt: Optional[str]) -> str:
        missing = [
            label for label, value in (
                ("name", name), ("schedule_type", schedule_type),
                ("schedule_value", schedule_value), ("prompt", prompt),
            ) if not value
        ]
        if missing:
            return f"Error: 缺少必填参数：{', '.join(missing)}。"

        if schedule_type not in _VALID_SCHEDULE_TYPES:
            return f"Error: schedule_type 无效，可选 {sorted(_VALID_SCHEDULE_TYPES)}。"

        active = [t for t in self._repository.load_all() if t.get("enabled", True)]
        if len(active) >= _MAX_ACTIVE_TASKS:
            return f"Error: 活动任务已达 {_MAX_ACTIVE_TASKS} 个上限，请先删除部分任务。"

        try:
            validated_value, first_next_run = self._validate_schedule(schedule_type, schedule_value)
        except _ScheduleError as err:
            return str(err)

        task_id = uuid.uuid4().hex[:8]
        now_iso = datetime.now().isoformat(timespec="seconds")
        task = {
            "id": task_id,
            "name": name,
            "schedule_type": schedule_type,
            "schedule_value": validated_value,
            "prompt": prompt,
            "enabled": True,
            "created_at": now_iso,
            "updated_at": now_iso,
            "next_run_at": first_next_run.isoformat(timespec="seconds") if first_next_run else None,
            "last_run_at": None,
            "last_error": None,
        }
        self._repository.add(task)
        next_repr = first_next_run.strftime("%Y-%m-%d %H:%M:%S") if first_next_run else "未知"
        return (
            f"Success: 已创建定时任务 {task_id}\n"
            f"  名称：{name}\n"
            f"  调度：{schedule_type} = {validated_value}\n"
            f"  下次触发：{next_repr}"
        )

    def _do_list(self) -> str:
        tasks = self._repository.load_all()
        if not tasks:
            return "当前没有任何定时任务。"
        lines = [f"共 {len(tasks)} 个任务："]
        for task in tasks:
            status = "启用" if task.get("enabled", True) else "已暂停"
            next_run = task.get("next_run_at") or "未知"
            lines.append(
                f"- [{task['id']}] {task.get('name', '(未命名)')} | {status} | "
                f"{task.get('schedule_type')}={task.get('schedule_value')} | 下次={next_run}"
            )
        return "\n".join(lines)

    def _do_delete(self, task_id: Optional[str]) -> str:
        if not task_id:
            return "Error: delete 需要提供 task_id。"
        ok = self._repository.remove(task_id)
        return f"Success: 已删除任务 {task_id}" if ok else f"Error: 未找到任务 {task_id}"

    def _do_set_enabled(self, task_id: Optional[str], enabled: bool) -> str:
        verb = "恢复" if enabled else "暂停"
        if not task_id:
            return f"Error: {verb}操作需要提供 task_id。"
        if not self._repository.get(task_id):
            return f"Error: 未找到任务 {task_id}"
        self._repository.update(task_id, {"enabled": enabled})
        return f"Success: 已{verb}任务 {task_id}"

    # ---------- 校验 ----------

    @staticmethod
    def _validate_schedule(schedule_type: str, schedule_value: str) -> tuple[str, Optional[datetime]]:
        now = datetime.now()
        if schedule_type == "interval":
            try:
                seconds = int(schedule_value)
            except (TypeError, ValueError):
                raise _ScheduleError("Error: interval 的 schedule_value 必须是整数秒数。")
            if seconds < _MIN_INTERVAL_SECONDS:
                raise _ScheduleError(
                    f"Error: interval 最小允许 {_MIN_INTERVAL_SECONDS} 秒，传入 {seconds}。"
                )
            return str(seconds), now + timedelta(seconds=seconds)

        if schedule_type == "once":
            target = _parse_once_value(schedule_value, now)
            if target is None:
                raise _ScheduleError(
                    "Error: once 的 schedule_value 必须是 ISO8601 时间或形如 +30s/+5m/+2h/+1d 的相对时间。"
                )
            if target <= now:
                raise _ScheduleError("Error: once 的目标时间已过去，无法触发。")
            return target.isoformat(timespec="seconds"), target

        if schedule_type == "cron":
            try:
                from croniter import croniter
            except ImportError:
                raise _ScheduleError(
                    "Error: 当前环境缺少 croniter，请执行 `pip install croniter` 后再使用 cron 调度。"
                )
            if not croniter.is_valid(schedule_value):
                raise _ScheduleError(f"Error: cron 表达式 '{schedule_value}' 不合法。")
            first = croniter(schedule_value, now).get_next(datetime)
            return schedule_value, first

        raise _ScheduleError(f"Error: 未知 schedule_type '{schedule_type}'。")


class _ScheduleError(Exception):
    """调度参数错误，message 直接回传 LLM"""


def _parse_once_value(value: str, now: datetime) -> Optional[datetime]:
    match = _RELATIVE_TIME_PATTERN.match(value or "")
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        unit_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
        return now + timedelta(**{unit_map[unit]: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed
