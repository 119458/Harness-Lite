"""定时任务持久化层

约定：
- 全部任务持久化在 `memory_store/scheduler/tasks.json`。
- 任务结构（dict）：
    {
        "id": "<8 字符随机>",
        "name": "任务名称",
        "schedule_type": "cron" | "interval" | "once",
        "schedule_value": "...",
        "prompt": "触发时要投递的指令",
        "enabled": true,
        "created_at": "ISO8601",
        "updated_at": "ISO8601",
        "next_run_at": "ISO8601" | null,
        "last_run_at": "ISO8601" | null,
        "last_error": null
    }
- 所有读写都加线程锁，避免被调度线程与用户调用线程冲突。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class TaskRepository:
    """任务仓库：基于 JSON 文件的简单 KV 存储"""

    def __init__(self, store_path: Path):
        self._store_path = Path(store_path)
        self._lock = threading.RLock()
        self._store_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def store_path(self) -> Path:
        return self._store_path

    # ---------- 读取 ----------

    def _read_all(self) -> Dict[str, dict]:
        if not self._store_path.exists():
            return {}
        try:
            with self._store_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                return payload.get("tasks", {})
            return {}
        except (OSError, json.JSONDecodeError):
            return {}

    def load_all(self) -> List[dict]:
        with self._lock:
            return list(self._read_all().values())

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._read_all().get(task_id)

    # ---------- 写入 ----------

    def _write_all(self, tasks: Dict[str, dict]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "tasks": tasks,
        }
        with self._store_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def add(self, task: dict) -> None:
        if "id" not in task:
            raise ValueError("任务缺少 id 字段")
        with self._lock:
            tasks = self._read_all()
            if task["id"] in tasks:
                raise ValueError(f"任务 id '{task['id']}' 已存在")
            tasks[task["id"]] = task
            self._write_all(tasks)

    def update(self, task_id: str, patch: dict) -> bool:
        with self._lock:
            tasks = self._read_all()
            if task_id not in tasks:
                return False
            merged = {**tasks[task_id], **patch, "updated_at": datetime.now().isoformat(timespec="seconds")}
            tasks[task_id] = merged
            self._write_all(tasks)
        return True

    def remove(self, task_id: str) -> bool:
        with self._lock:
            tasks = self._read_all()
            if task_id not in tasks:
                return False
            del tasks[task_id]
            self._write_all(tasks)
        return True


_DISPATCH_LOG_FILENAME = "pending_prompts.json"


def append_pending_prompt(store_dir: Path, payload: dict) -> None:
    """把待投递的 prompt 写到一个 JSON 数组文件，等后续阶段实现真正的注入

    本阶段先用文件做投递通道，便于人工观察 / 调试。线程安全由调用方保证。
    """
    target = Path(store_dir) / _DISPATCH_LOG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    existing = data
        except (OSError, json.JSONDecodeError):
            existing = []
    existing.append(payload)
    with target.open("w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=2)
