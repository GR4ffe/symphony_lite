"""
Shared fixtures for symphony_core testing.

Provides MockOrchestrator builder that creates a SymphonyOrchestrator
with all external dependencies mocked (TasksDB, WorkspaceManager,
Notifier, MemoryManager, AgentAdapter).
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# Initialize log BEFORE importing SymphonyOrchestrator
# to prevent 'log is None' errors
import FRAMEWORK.logger as _logger_mod

_logger_mod.init_logger("/tmp/symphony_test_symphony.log", level="CRITICAL")

# Add FRAMEWORK dir to path for intra-package imports like 'from workspace_mgr import ...'
_framework_dir = os.path.join(os.path.dirname(__file__), "..", "..", "FRAMEWORK")
if _framework_dir not in sys.path:
    sys.path.insert(0, _framework_dir)

from FRAMEWORK.config_loader import Config
from FRAMEWORK.symphony_core import RetryEntry, RunningEntry, SymphonyOrchestrator
from FRAMEWORK.tasks_db import Task


def make_minimal_config() -> Config:
    """创建一个最小配置，所有路径指向 /tmp"""
    return Config._from_dict({
        "tracker": {
            "tasks_file": "/tmp/symphony_test/tasks.json",
            "active_states": ["Todo", "In Progress"],
            "terminal_states": ["Done", "Canceled"],
        },
        "polling": {"interval_ms": 1000},
        "workspace": {"root": "/tmp/symphony_test/ws"},
        "agent": {
            "executor": "opencode",
            "max_concurrent_agents": 3,
            "max_retry_backoff_ms": 60000,
        },
        "timeouts": {
            "stall_timeout_ms": 300000,
        },
        "memory": {
            "constitution_file": "/tmp/constitution.md",
            "index_root": "/tmp/memory_index",
        },
        "logging": {
            "file": "/tmp/symphony_test/symphony.log",
            "level": "INFO",
        },
        "notification": {
            "sillack_web_url": "http://localhost:1",
            "inbox_dir": "/tmp/symphony_test/inbox",
        },
    })


def make_running_entry(task_id: str = "T-1", attempt: int = 1) -> RunningEntry:
    """创建一个 RunningEntry（用于测试）"""
    return RunningEntry(
        task_id=task_id,
        identifier=task_id,
        workspace=MagicMock(path=f"/tmp/ws/{task_id}"),
        session=MagicMock(pid=12345, id=f"{task_id}-sess"),
        started_at=datetime.now(timezone.utc),
        last_event="session_started",
        attempt=attempt,
    )


def make_retry_entry(task_id: str = "T-1", attempt: int = 1) -> RetryEntry:
    """创建一个 RetryEntry（用于测试）"""
    return RetryEntry(
        task_id=task_id,
        identifier=task_id,
        attempt=attempt,
        due_at_ms=0,  # 立即到期
        error="test error",
    )


def make_task(task_id: str = "T-1", state: str = "Todo", **overrides) -> Task:
    """创建测试用 Task"""
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


@pytest.fixture
def mock_orchestrator():
    """
    创建一个完全 mock 化的 orchestrator。
    - tasks_db: MagicMock
    - workspace_mgr: MagicMock
    - agent_adapter: MagicMock
    - notifier: MagicMock
    - memory_mgr: MagicMock
    """
    config = make_minimal_config()
    orch = SymphonyOrchestrator(config)
    orch.tasks_db = MagicMock()
    orch.workspace_mgr = MagicMock()
    orch.agent_adapter = MagicMock()
    orch.notifier = MagicMock()
    orch.memory_mgr = MagicMock()
    orch.instance_id = "test-host-99999"

    # Default mock behaviors
    orch.agent_adapter.is_process_alive.return_value = True
    orch.agent_adapter.wait_session.return_value = MagicMock(
        status="success", exit_code=0, stdout="done", stderr="", result={}
    )
    orch.agent_adapter.start_session.return_value = MagicMock(pid=99999, id="sess-1")
    orch.tasks_db.load_all_tasks.return_value = []
    orch.tasks_db.fetch_candidate_issues.return_value = []
    orch.tasks_db.get_task.return_value = None
    orch.tasks_db.is_blacklisted.return_value = False
    # Return real strings (MagicMock __add__ returns MagicMock, which breaks "".join())
    orch.memory_mgr.build_filtered_context.return_value = ""

    return orch
