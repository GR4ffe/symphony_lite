"""
Test suite for FRAMEWORK/tasks_db.py

Covers:
- Task dataclass: construction, to_dict/from_dict, identifier, forward compatibility
- TasksDB: init, _ensure_file, CRUD (add/update/remove/load)
- TasksDB query: get_task, get_state, fetch_candidate_issues, fetch_retrying_tasks
- TasksDB edge cases: update non-existent task, error accumulation, attempt_count monotonic
- TasksDB retry/blacklist: update_retry_state, blacklist_task, is_blacklisted
"""

import json
import os

from FRAMEWORK.tasks_db import Task, TasksDB

# ── 辅助函数 ────────────────────────────────────────────────────


def make_task(task_id: str = "TASK-001", state: str = "Todo", **overrides) -> Task:
    """创建测试用 Task，可覆盖任意字段"""
    defaults = {
        "id": task_id,
        "title": f"Test {task_id}",
        "description": f"Description for {task_id}",
        "state": state,
        "priority": 1,
        "topic": "test",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "acquired_at": None,
        "acquired_by": None,
        "lock_pid": None,
        "attempt_count": 0,
        "error": None,
        "result": None,
        "metadata": {},
    }
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore


# ── Task dataclass ─────────────────────────────────────────────


class TestTask:
    def test_construct_minimal(self):
        """Task 最少参数构造（含默认值字段）"""
        t = Task(
            id="T-1", title="t", description="d", state="Todo",
            priority=None, topic=None, created_at="now", updated_at="now",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        assert t.id == "T-1"
        assert t.state == "Todo"
        assert t.retry_after is None
        assert t.is_retrying is False
        assert t.blacklisted is False

    def test_identifier(self):
        """identifier 返回 task_id"""
        t = make_task("TASK-X")
        assert t.identifier == "TASK-X"

    def test_to_dict(self):
        """to_dict 返回包含所有字段的字典"""
        t = make_task("T-1")
        d = t.to_dict()
        assert d["id"] == "T-1"
        assert d["state"] == "Todo"
        assert d["retry_after"] is None

    def test_from_dict(self):
        """from_dict 正确反序列化"""
        d = {
            "id": "T-2", "title": "task2", "description": "desc", "state": "Done",
            "priority": 2, "topic": "bugs", "created_at": "now", "updated_at": "now",
            "acquired_at": None, "acquired_by": None, "lock_pid": None,
            "attempt_count": 1, "error": None, "result": {}, "metadata": {"key": "val"},
            "retry_after": None, "is_retrying": False, "blacklisted": False,
        }
        t = Task.from_dict(d)
        assert t.id == "T-2"
        assert t.state == "Done"
        assert t.metadata["key"] == "val"

    def test_from_dict_ignores_unknown_fields(self):
        """from_dict 忽略未知字段（向前兼容）"""
        d = make_task("T-3").to_dict()
        d["unknown_field"] = "should_be_ignored"
        d["another_unknown"] = 42
        t = Task.from_dict(d)
        assert t.id == "T-3"
        assert not hasattr(t, "unknown_field")


# ── TasksDB init ────────────────────────────────────────────────


class TestTasksDBInit:
    def test_auto_creates_file(self, temp_dir):
        """文件不存在时自动创建"""
        db_file = str(temp_dir / "tasks.json")
        TasksDB(db_file)
        assert os.path.exists(db_file)
        with open(db_file) as f:
            data = json.load(f)
        assert data["schema_version"] == "1.0"
        assert data["tasks"] == []

    def test_loads_existing_file(self, temp_dir):
        """已存在的文件正常加载"""
        db_file = str(temp_dir / "tasks.json")
        with open(db_file, "w") as f:
            json.dump({"schema_version": "1.0", "tasks": [make_task("T-1").to_dict()]}, f)
        db = TasksDB(db_file)
        assert len(db.load_all_tasks()) == 1


# ── TasksDB CRUD ────────────────────────────────────────────────


class TestTasksDBCRUD:
    def test_add_and_load(self, temp_dir):
        """add_task 后 load_all_tasks 返回正确"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        tasks = db.load_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "T-1"

    def test_add_multiple_tasks(self, temp_dir):
        """添加多个任务"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        for i in range(3):
            db.add_task(make_task(f"T-{i}"))
        assert len(db.load_all_tasks()) == 3

    def test_update_task_field(self, temp_dir):
        """update_task 更新指定字段"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", state="Todo"))
        db.update_task("T-1", {"state": "In Progress"})
        t = db.get_task("T-1")
        assert t is not None
        assert t.state == "In Progress"

    def test_update_multiple_fields(self, temp_dir):
        """一次更新多个字段"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        db.update_task("T-1", {"state": "Done", "priority": 10})
        t = db.get_task("T-1")
        assert t.state == "Done"
        assert t.priority == 10

    def test_update_updates_timestamp(self, temp_dir):
        """update_task 会刷新 updated_at"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", updated_at="2000-01-01T00:00:00Z"))
        db.update_task("T-1", {"state": "Done"})
        t = db.get_task("T-1")
        assert t.updated_at > "2000-01-01"

    def test_remove_task(self, temp_dir):
        """remove_task 删除指定任务"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        db.add_task(make_task("T-2"))
        db.remove_task("T-1")
        tasks = db.load_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "T-2"

    def test_get_task_not_found(self, temp_dir):
        """get_task 不存在的任务返回 None"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        assert db.get_task("NONEXISTENT") is None


# ── TasksDB query ────────────────────────────────────────────────


class TestTasksDBQuery:
    def test_get_state(self, temp_dir):
        """get_state 只返回 state 字段"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", state="In Progress"))
        assert db.get_state("T-1") == "In Progress"

    def test_get_state_not_found(self, temp_dir):
        """get_state 不存在返回空字符串"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        assert db.get_state("NONEXISTENT") == ""

    def test_fetch_candidate_issues(self, temp_dir):
        """fetch_candidate_issues 只返回 active_states 中的任务"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", state="Todo"))
        db.add_task(make_task("T-2", state="In Progress"))
        db.add_task(make_task("T-3", state="Done"))
        db.add_task(make_task("T-4", state="Canceled"))
        candidates = db.fetch_candidate_issues(["Todo", "In Progress"])
        assert len(candidates) == 2
        assert {t.id for t in candidates} == {"T-1", "T-2"}


# ── Edge cases ──────────────────────────────────────────────────


class TestTasksDBEdgeCases:
    def test_update_nonexistent_task_appends(self, temp_dir):
        """更新不存在的任务时防御性追加"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.update_task("T-NEW", {"state": "Todo", "title": "auto created"})
        t = db.get_task("T-NEW")
        assert t is not None
        assert t.title == "auto created"

    def test_error_accumulation(self, temp_dir):
        """error 字段累加而非覆盖"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        db.update_task("T-1", {"error": "first error"})
        db.update_task("T-1", {"error": "second error"})
        t = db.get_task("T-1")
        assert "first error" in t.error
        assert "second error" in t.error

    def test_attempt_count_monotonic(self, temp_dir):
        """attempt_count 只增不减"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", attempt_count=5))
        db.update_task("T-1", {"attempt_count": 3})  # 更小，应忽略
        t = db.get_task("T-1")
        assert t.attempt_count == 5
        db.update_task("T-1", {"attempt_count": 10})  # 更大，应更新
        t = db.get_task("T-1")
        assert t.attempt_count == 10

    def test_corrupted_json_resets(self, temp_dir):
        """损坏的 JSON 自动重置为空"""
        db_file = str(temp_dir / "tasks.json")
        with open(db_file, "w") as f:
            f.write("{corrupted json!!!")
        db = TasksDB(db_file)
        tasks = db.load_all_tasks()
        assert tasks == []

    def test_atomic_write_creates_tmp(self, temp_dir):
        """原子写入先写.tmp再rename，完成后.tmp不留"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        # .tmp 文件应当被 rename 走
        tmp_file = str(temp_dir / "tasks.json.tmp")
        assert not os.path.exists(tmp_file)
        # 主文件存在
        assert os.path.exists(str(temp_dir / "tasks.json"))


# ── Retry & Blacklist ──────────────────────────────────────────


class TestTasksDBRetry:
    def test_update_retry_state(self, temp_dir):
        """update_retry_state 设置退避状态"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        db.update_retry_state("T-1", "2026-12-31T23:59:59Z", True)
        t = db.get_task("T-1")
        assert t.is_retrying is True
        assert t.retry_after == "2026-12-31T23:59:59Z"

    def test_fetch_retrying_tasks(self, temp_dir):
        """fetch_retrying_tasks 只返回 is_retrying=True 的任务"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", state="Todo"))
        db.add_task(make_task("T-2", is_retrying=True))
        retrying = db.fetch_retrying_tasks()
        assert len(retrying) == 1
        assert retrying[0].id == "T-2"

    def test_blacklist(self, temp_dir):
        """blacklist_task 将任务加入黑名单"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1", is_retrying=True))
        db.blacklist_task("T-1")
        t = db.get_task("T-1")
        assert t.blacklisted is True
        assert t.is_retrying is False
        assert t.retry_after is None

    def test_is_blacklisted(self, temp_dir):
        """is_blacklisted 返回正确状态"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        db.add_task(make_task("T-1"))
        assert db.is_blacklisted("T-1") is False
        db.blacklist_task("T-1")
        assert db.is_blacklisted("T-1") is True

    def test_is_blacklisted_not_found(self, temp_dir):
        """不存在的任务返回 False"""
        db = TasksDB(str(temp_dir / "tasks.json"))
        assert db.is_blacklisted("NONEXISTENT") is False
