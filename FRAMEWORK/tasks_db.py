"""
Symphony-Lite 任务数据库
FRAMEWORK/tasks_db.py

读写 tasks.json，提供类型化查询接口。
所有写操作直接落盘，保证 Hermes 重启后能对账。
"""
import fcntl
import json
import os
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from typing import Any


@dataclass
class Task:
    id: str
    title: str
    description: str
    state: str
    priority: int | None
    topic: str | None
    created_at: str
    updated_at: str
    acquired_at: str | None
    acquired_by: str | None
    lock_pid: int | None
    attempt_count: int
    error: str | None
    result: dict | None
    metadata: dict
    # I5：重试状态持久化——重启后重建 retry 队列
    retry_after: str | None = None  # ISO 时间戳：最早允许重新调度的时刻
    is_retrying: bool = False       # 当前是否处于退避等待中
    # FIX-003：黑名单——被放弃的任务禁止重试，防止 giveup 后又被 dispatch
    blacklisted: bool = False

    @property
    def identifier(self) -> str:
        """用于排序的唯一标识（task_id）"""
        return self.id

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Task":
        # 过滤未知字段（向前兼容）
        known = {f.name for f in fields(Task)}
        filtered = {k: v for k, v in d.items() if k in known}
        return Task(**filtered)


class TasksDB:
    """
    tasks.json 读写接口。

    文件格式：
    {
      "schema_version": "1.0",
      "tasks": [Task, ...]
    }
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, tasks_file: str):
        self.tasks_file = tasks_file
        self._ensure_file()

    def _ensure_file(self) -> None:
        """如果文件不存在，初始化空结构"""
        if not os.path.exists(self.tasks_file):
            os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
            self._write_raw({"schema_version": self.SCHEMA_VERSION, "tasks": []})

    def _read_raw(self) -> dict:
        """读取原始 JSON"""
        try:
            with open(self.tasks_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            # 文件损坏或被删，重置
            return {"schema_version": self.SCHEMA_VERSION, "tasks": []}

    def _write_raw(self, raw: dict) -> None:
        """写入原始 JSON（原子操作：先写.tmp再rename）

        FIX-002: 加独占文件锁，防止多实例并发写入导致 rename 竞争。
        锁在写入期间持有，Linux 下进程退出时自动释放。
        """
        tmp = self.tasks_file + ".tmp"
        # 打开锁文件（与 tasks.json 同目录）
        lock_file = self.tasks_file + ".lock"
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.tasks_file)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    # ── 读 ──────────────────────────────────────────────────────────────────

    def load_all_tasks(self) -> list[Task]:
        """启动时全量加载"""
        raw = self._read_raw()
        return [Task.from_dict(t) for t in raw.get("tasks", [])]

    def get_task(self, task_id: str) -> Task | None:
        """根据 ID 查单一任务"""
        tasks = self.load_all_tasks()
        return next((t for t in tasks if t.id == task_id), None)

    def get_state(self, task_id: str) -> str:
        """
        只查 state 字段（用于外部对账，不全量加载）。
        返回空字符串表示任务不存在。
        """
        raw = self._read_raw()
        for t in raw.get("tasks", []):
            if t["id"] == task_id:
                return t["state"]
        return ""

    def fetch_candidate_issues(self, active_states: list[str]) -> list[Task]:
        """
        查询处于 active_states 的任务（用于调度池）。
        不包含 Pending Review / Done / Canceled。
        """
        tasks = self.load_all_tasks()
        return [t for t in tasks if t.state in active_states]

    # ── 写 ──────────────────────────────────────────────────────────────────

    def update_task(self, task_id: str, updates: dict) -> None:
        """
        原子更新指定 task 的字段，保留其他字段不变。
        先读 → 改 → 写回全量文件。

        特殊规则：
        - error 字段：累加而非覆盖（多条错误历史）
        - attempt_count：只增不减
        """
        raw = self._read_raw()

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        for t in raw["tasks"]:
            if t["id"] == task_id:
                for key, value in updates.items():
                    if key == "error" and value:
                        # 累加错误历史
                        old_err = t.get("error") or ""
                        t["error"] = old_err + (" | " if old_err else "") + value
                    elif key == "attempt_count":
                        # 只增不减
                        t["attempt_count"] = max(t.get("attempt_count", 0), value)
                    else:
                        t[key] = value
                t["updated_at"] = now
                break
        else:
            # 任务不存在，追加（不应该发生，但防御性处理）
            new_task = {
                "id": task_id,
                "title": "",
                "description": "",
                "state": "Todo",
                "priority": None,
                "topic": None,
                "created_at": now,
                "updated_at": now,
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
                "attempt_count": 0,
                "error": None,
                "result": None,
                "metadata": {},
            }
            new_task.update(updates)
            new_task["updated_at"] = now
            raw["tasks"].append(new_task)

        self._write_raw(raw)

    def add_task(self, task: Task) -> None:
        """添加一个新任务"""
        raw = self._read_raw()
        raw["tasks"].append(task.to_dict())
        self._write_raw(raw)

    def remove_task(self, task_id: str) -> None:
        """删除一个任务"""
        raw = self._read_raw()
        raw["tasks"] = [t for t in raw["tasks"] if t["id"] != task_id]
        self._write_raw(raw)

    # I5：重试状态持久化
    def fetch_retrying_tasks(self) -> list["Task"]:
        """查询处于退避等待中的任务（is_retrying=True）"""
        tasks = self.load_all_tasks()
        return [t for t in tasks if getattr(t, 'is_retrying', False)]

    def is_blacklisted(self, task_id: str) -> bool:
        """检查任务是否在黑名单中（FIX-003）"""
        raw = self._read_raw()
        for t in raw.get("tasks", []):
            if t["id"] == task_id:
                return t.get("blacklisted", False)
        return False

    def update_retry_state(self, task_id: str, retry_after: str | None, is_retrying: bool) -> None:
        """更新任务的退避状态（不触动其他字段）"""
        self.update_task(task_id, {
            "retry_after": retry_after,
            "is_retrying": is_retrying,
        })

    def blacklist_task(self, task_id: str) -> None:
        """将任务加入黑名单，禁止重试（FIX-003）"""
        self.update_task(task_id, {
            "blacklisted": True,
            "is_retrying": False,
            "retry_after": None,
        })
