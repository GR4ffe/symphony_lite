"""
Integration tests for Symphony-Lite orchestrator.

Tests the full pipeline with real TasksDB (file I/O) but mocked agent adapter:
1. Write task → orchestrator dispatch → state = In Progress
2. Mock agent completes → verify Pending Review
3. Recovery: orchestrator restart with In Progress task → verify reset
4. Full state machine: Todo → In Progress → Pending Review → Done
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Initialize log BEFORE importing SymphonyOrchestrator
import FRAMEWORK.logger as _int_logger

_int_logger.init_logger("/tmp/symphony_integration_test.log", level="CRITICAL")

from FRAMEWORK.agent_types import AgentResult, Session
from FRAMEWORK.config_loader import Config
from FRAMEWORK.symphony_core import SymphonyOrchestrator
from FRAMEWORK.tasks_db import Task, TasksDB


@pytest.fixture
def integration_env(tmp_path):
    """
    创建完整的集成测试环境：
    - tmp_path 下的 symphony_data/ 目录结构
    - 真实 Config 和 TasksDB
    - 所有外部依赖 mocked (agent, workspace, notifier, memory)
    - log 已初始化
    """
    import FRAMEWORK.logger as lm
    lm.init_logger(str(tmp_path / "symphony.log"), level="CRITICAL")

    # 确保 FRAMEWORK/ 在 sys.path 中（解决 "from workspace_mgr import ..." 问题）
    import sys
    _fw_dir = os.path.join(os.path.dirname(__file__), "..", "FRAMEWORK")
    if _fw_dir not in sys.path:
        sys.path.insert(0, _fw_dir)

    # 创建目录结构
    data_dir = tmp_path / "symphony_data"
    data_dir.mkdir()
    (data_dir / "logs").mkdir()
    (data_dir / "inbox").mkdir()
    (data_dir / "workspace_root").mkdir()
    (data_dir / "memory_index").mkdir()

    config = Config._from_dict({
        "tracker": {
            "tasks_file": str(data_dir / "tasks.json"),
            "active_states": ["Todo", "In Progress"],
            "terminal_states": ["Done", "Canceled"],
        },
        "polling": {"interval_ms": 1000},
        "workspace": {"root": str(data_dir / "workspace_root")},
        "agent": {
            "executor": "opencode",
            "max_concurrent_agents": 3,
            "max_retry_backoff_ms": 60000,
        },
        "timeouts": {"stall_timeout_ms": 300000},
        "memory": {
            "constitution_file": str(data_dir / "constitution.md"),
            "index_root": str(data_dir / "memory_index"),
        },
        "logging": {
            "file": str(data_dir / "logs" / "symphony.log"),
            "level": "CRITICAL",
        },
        "notification": {
            "sillack_web_url": "http://localhost:1",
            "inbox_dir": str(data_dir / "inbox"),
        },
    })

    # 创建真实 TasksDB
    tasks_db = TasksDB(str(data_dir / "tasks.json"))

    # 创建 orchestrator 但手动注入 mock 依赖
    orch = SymphonyOrchestrator(config)
    orch.tasks_db = tasks_db
    orch.workspace_mgr = MagicMock()
    orch.agent_adapter = MagicMock()
    orch.notifier = MagicMock()
    orch.memory_mgr = MagicMock()
    orch.instance_id = "integration-test-host-1"
    orch.file_watcher = None

    # Mock behaviors
    orch.agent_adapter.is_process_alive.return_value = True
    orch.agent_adapter.start_session.return_value = Session(id="sess-int-1", pid=99999)
    orch.agent_adapter.wait_session.return_value = AgentResult(
        status="success", exit_code=0, stdout="task done", stderr="", result={"key": "val"}
    )
    orch.workspace_mgr.prepare.return_value = MagicMock(
        path=str(data_dir / "workspace_root" / "TASK-INT-001")
    )
    # 创建真实 workspace 目录，让 _on_agent_done 能检测到输出文件
    ws_dir = data_dir / "workspace_root"
    ws_dir.mkdir(parents=True, exist_ok=True)
    # _on_agent_done 读取 entry.workspace.path 来检测输出文件
    # 使用 mock 的 os.listdir 会让测试绕过真实文件系统
    # 更好的方式：创建目录并写入输出文件
    for task_path in [ws_dir / "TASK-INT-001", ws_dir / "TASK-FILE-001",
                      ws_dir / "TASK-NO-DUP", ws_dir / "T-CAP-001"]:
        task_path.mkdir(exist_ok=True)
        (task_path / "_output.md").write_text("test output content")

    orch.memory_mgr.build_filtered_context.return_value = ""

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "config": config,
        "tasks_db": tasks_db,
        "orchestrator": orch,
    }


class TestIntegrationPipeline:
    def test_tick_dispatches_task_and_changes_state(self, integration_env):
        """写一个 Todo 任务 → tick → 状态变 In Progress"""
        orch = integration_env["orchestrator"]
        tasks_db = integration_env["tasks_db"]

        # 写一个真实任务到 tasks.json
        task = Task(
            id="TASK-INT-001", title="Integration Test", description="Do the thing",
            state="Todo", priority=1, topic="test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        tasks_db.add_task(task)

        # Tick → dispatch
        with patch.object(orch, "_build_prompt", return_value="execute task"):
            orch._tick(1)

        # 验证状态
        updated = tasks_db.get_task("TASK-INT-001")
        assert updated is not None
        assert updated.state == "In Progress"

    def test_dispatch_writes_to_tasks_json(self, integration_env):
        """dispatch 后 tasks.json 文件内容正确"""
        orch = integration_env["orchestrator"]
        tasks_db = integration_env["tasks_db"]
        data_dir = integration_env["data_dir"]

        task = Task(
            id="TASK-FILE-001", title="File Check", description="Write test",
            state="Todo", priority=1, topic="test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        tasks_db.add_task(task)

        with patch.object(orch, "_build_prompt", return_value="execute"):
            orch._tick(1)

        # 直接读文件验证
        with open(data_dir / "tasks.json") as f:
            raw = json.load(f)

        target = next(t for t in raw["tasks"] if t["id"] == "TASK-FILE-001")
        assert target["state"] == "In Progress"
        assert target["acquired_by"] == "integration-test-host-1"
        assert target["lock_pid"] == 99999

    def test_multiple_ticks_no_duplicate_dispatch(self, integration_env):
        """多次 tick 不重复派发已 In Progress 的任务"""
        orch = integration_env["orchestrator"]
        tasks_db = integration_env["tasks_db"]

        task = Task(
            id="TASK-NO-DUP", title="No Duplicate", description="Test",
            state="Todo", priority=1, topic="test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        tasks_db.add_task(task)

        with patch.object(orch, "_build_prompt", return_value="execute"):
            orch._tick(1)
            orch._tick(2)
            orch._tick(3)

        # start_session 应该只调用一次
        assert orch.agent_adapter.start_session.call_count == 1

    def test_empty_tasks_no_error(self, integration_env):
        """没有任务时 tick 不报错"""
        orch = integration_env["orchestrator"]
        orch._tick(1)  # should not raise

    def test_max_concurrent_respected(self, integration_env):
        """超过并发上限不派发新任务"""
        orch = integration_env["orchestrator"]
        tasks_db = integration_env["tasks_db"]
        orch.config.agent.max_concurrent_agents = 1

        # 第一个任务
        t1 = Task(
            id="T-CAP-001", title="First", description="Do it",
            state="Todo", priority=1, topic="test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        tasks_db.add_task(t1)

        with patch.object(orch, "_build_prompt", return_value="execute"):
            orch._tick(1)  # 派发第一个

        # 模拟第一个还在 running 中
        # (实际上 _agent_worker 线程立即完成了，但为测试需要模拟)
        from datetime import datetime, timezone

        from FRAMEWORK.symphony_core import RunningEntry
        orch.running["T-CAP-001"] = RunningEntry(
            task_id="T-CAP-001", identifier="T-CAP-001",
            workspace=MagicMock(path="/ws/T-CAP-001"),
            session=MagicMock(pid=11111, id="sess-1"),
            started_at=datetime.now(timezone.utc),
            last_event="running", attempt=1,
        )
        orch.claimed.add("T-CAP-001")

        # 第二个任务
        t2 = Task(
            id="T-CAP-002", title="Second", description="Should not dispatch",
            state="Todo", priority=2, topic="test",
            created_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            acquired_at=None, acquired_by=None, lock_pid=None,
            attempt_count=0, error=None, result=None, metadata={},
        )
        tasks_db.add_task(t2)

        start_session_count_before = orch.agent_adapter.start_session.call_count
        orch._tick(2)
        # 第二个任务不应派发
        assert orch.agent_adapter.start_session.call_count == start_session_count_before
