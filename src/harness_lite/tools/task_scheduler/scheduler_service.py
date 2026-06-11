"""定时任务的后台调度服务

设计要点：
- 全局单例，进程内只跑一个调度线程，避免重复触发。
- 每 30 秒巡检一次，遇到 next_run_at <= now 的任务就触发。
- 触发逻辑：
    1. 把当前任务的 prompt 与元数据写入 pending_prompts.json（待 ReAct 投递通道接入后由它消费）。
    2. 打印日志，记录 last_run_at。
    3. 重新计算 next_run_at（cron / interval）或删除任务（once）。
- 逾期超过 10 分钟才发现的任务：cron/interval 自动跳过本次并对齐到下一个时间点，
  避免一次性补发大量历史触发。

TODO：后续阶段需要让 ReAct 引擎主动消费 pending_prompts.json，
       将触发的指令重新喂回 LLM。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .task_store import TaskRepository, append_pending_prompt

_POLL_INTERVAL_SECONDS = 30
_OVERDUE_TOLERANCE_SECONDS = 600  # 10 分钟


class TaskDispatcher:
    """后台调度器，单例使用"""

    _singleton: Optional["TaskDispatcher"] = None
    _singleton_lock = threading.Lock()

    def __init__(self, repository: TaskRepository):
        self._repo = repository
        self._dispatch_dir = repository.store_path.parent
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()

    @classmethod
    def get_or_create(cls, repository: TaskRepository) -> "TaskDispatcher":
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls(repository)
            return cls._singleton

    # ---------- 启停 ----------

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="HarnessTaskDispatcher", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            self._stop_event.set()
            target = self._thread
        if target and target.is_alive():
            target.join(timeout=timeout)

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                print(f"[TaskDispatcher] tick 异常：{exc}")
            self._stop_event.wait(_POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        now = datetime.now()
        for task in self._repo.load_all():
            if not task.get("enabled", True):
                continue
            try:
                self._handle_one(task, now)
            except Exception as exc:  # noqa: BLE001
                self._repo.update(task["id"], {
                    "last_error": str(exc),
                })

    def _handle_one(self, task: dict, now: datetime) -> None:
        next_run_str = task.get("next_run_at")
        if not next_run_str:
            new_next = self._compute_next_run(task, now)
            if new_next:
                self._repo.update(task["id"], {"next_run_at": new_next.isoformat(timespec="seconds")})
            return

        next_run = _parse_iso(next_run_str)
        if next_run is None:
            # 损坏的时间戳，重新对齐
            new_next = self._compute_next_run(task, now)
            self._repo.update(task["id"], {"next_run_at": new_next.isoformat(timespec="seconds") if new_next else None})
            return

        if next_run > now:
            return  # 尚未到期

        overdue_seconds = (now - next_run).total_seconds()
        schedule_type = task.get("schedule_type")
        if overdue_seconds > _OVERDUE_TOLERANCE_SECONDS and schedule_type != "once":
            new_next = self._compute_next_run(task, now)
            self._repo.update(task["id"], {
                "next_run_at": new_next.isoformat(timespec="seconds") if new_next else None,
            })
            return

        self._fire_task(task, now)

    # ---------- 触发逻辑 ----------

    def _fire_task(self, task: dict, now: datetime) -> None:
        try:
            append_pending_prompt(self._dispatch_dir, {
                "task_id": task["id"],
                "name": task.get("name", ""),
                "prompt": task.get("prompt", ""),
                "fired_at": now.isoformat(timespec="seconds"),
            })
            print(f"[TaskDispatcher] 触发任务 {task['id']} - {task.get('name')}")
        except Exception as exc:  # noqa: BLE001
            self._repo.update(task["id"], {"last_error": f"投递失败: {exc}"})
            return

        # 计算下一次或删除一次性任务
        if task.get("schedule_type") == "once":
            self._repo.remove(task["id"])
            return

        new_next = self._compute_next_run(task, now)
        self._repo.update(task["id"], {
            "last_run_at": now.isoformat(timespec="seconds"),
            "next_run_at": new_next.isoformat(timespec="seconds") if new_next else None,
            "last_error": None,
        })

    # ---------- next_run 计算 ----------

    @staticmethod
    def _compute_next_run(task: dict, base: datetime) -> Optional[datetime]:
        schedule_type = task.get("schedule_type")
        schedule_value = task.get("schedule_value")

        if schedule_type == "interval":
            try:
                seconds = int(schedule_value)
            except (TypeError, ValueError):
                return None
            if seconds <= 0:
                return None
            return base + timedelta(seconds=seconds)

        if schedule_type == "once":
            return _parse_iso(str(schedule_value)) if schedule_value else None

        if schedule_type == "cron":
            try:
                from croniter import croniter  # 延迟导入，缺失时上层有提示
                cron = croniter(str(schedule_value), base)
                return cron.get_next(datetime)
            except Exception:  # noqa: BLE001
                return None

        return None


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed
