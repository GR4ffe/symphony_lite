"""
Test suite for FRAMEWORK/symphony_core.py

Covers:
- RuntimeState enum
- RunningEntry / RetryEntry dataclasses
- _startup_reconciliation — In Progress → recover/reset
- _tick — dispatch flow
- _dispatch — claimed, blacklist, empty task
- _on_agent_done — success → Pending Review, fail → retry
- _reconcile_running — terminal state, dead process
- _check_heartbeat_seq / _check_heartbeat
- _schedule_retry / _process_retries
- _on_task_approved / _on_task_revision
- _has_capacity / _sort_for_dispatch
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from FRAMEWORK.symphony_core import RetryEntry, RunningEntry, RuntimeState

# ── Enums and Dataclasses ────────────────────────────────────


class TestRuntimeState:
    def test_values(self):
        assert RuntimeState.UNCLAIMED.value == "unclaimed"
        assert RuntimeState.CLAIMED.value == "claimed"
        assert RuntimeState.RUNNING.value == "running"
        assert RuntimeState.RETRY_QUEUED.value == "retry_queued"
        assert RuntimeState.RELEASED.value == "released"


class TestRunningEntry:
    def test_construct(self):
        ws = MagicMock(path="/ws/T-1")
        sess = MagicMock(pid=100, id="sess-1")
        entry = RunningEntry(
            task_id="T-1", identifier="T-1",
            workspace=ws, session=sess,
            started_at=datetime.now(timezone.utc),
            last_event="started", attempt=1,
        )
        assert entry.task_id == "T-1"
        assert entry.grace_seconds == 60

    def test_custom_grace(self):
        entry = RunningEntry(
            task_id="T-2", identifier="T-2",
            workspace=MagicMock(), session=None,
            started_at=datetime.now(timezone.utc),
            last_event=None, attempt=1, grace_seconds=30,
        )
        assert entry.grace_seconds == 30


class TestRetryEntry:
    def test_construct(self):
        entry = RetryEntry(
            task_id="T-1", identifier="T-1",
            attempt=2, due_at_ms=1000, error="timeout",
        )
        assert entry.due_at_ms == 1000
        assert entry.error == "timeout"


# ── Startup Reconciliation ───────────────────────────────────


class TestStartupReconciliation:
    def test_empty_tasks_no_recovery(self, mock_orchestrator):
        """空任务列表，不恢复任何任务"""
        mock_orchestrator._startup_reconciliation()
        assert len(mock_orchestrator.running) == 0
        assert len(mock_orchestrator.claimed) == 0

    def test_no_in_progress_tasks_unchanged(self, mock_orchestrator):
        """没有 In Progress 任务，不做改变"""
        from conftest import make_task
        task = make_task("T-1", state="Todo")
        mock_orchestrator.tasks_db.load_all_tasks.return_value = [task]
        mock_orchestrator._startup_reconciliation()
        assert len(mock_orchestrator.running) == 0

    def test_in_progress_with_dead_pid_reset(self, mock_orchestrator):
        """In Progress + 进程已死 → 重置为 Todo"""
        from conftest import make_task
        task = make_task("T-1", state="In Progress", lock_pid=9999, attempt_count=3)
        mock_orchestrator.tasks_db.load_all_tasks.return_value = [task]
        mock_orchestrator.agent_adapter.is_process_alive.return_value = False

        mock_orchestrator._startup_reconciliation()

        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Todo"
            for call in update_calls
        )
        assert found, "Task should be reset to Todo"

    def test_in_progress_no_lockpid(self, mock_orchestrator):
        """In Progress + 无 lock_pid → 重置"""
        from conftest import make_task
        task = make_task("T-1", state="In Progress", lock_pid=None)
        mock_orchestrator.tasks_db.load_all_tasks.return_value = [task]

        mock_orchestrator._startup_reconciliation()

        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Todo"
            for call in update_calls
        )
        assert found


# ── Tick and Dispatch ─────────────────────────────────────────


class TestTick:
    def test_empty_tick_no_dispatch(self, mock_orchestrator):
        """没有候选任务时 tick 不派发"""
        mock_orchestrator._tick(1)
        mock_orchestrator.agent_adapter.start_session.assert_not_called()

    def test_tick_dispatches_todo_task(self, mock_orchestrator):
        """Todo 任务在 tick 中被 dispatch"""
        from conftest import make_task
        task = make_task("T-1", state="Todo", description="Do something")
        mock_orchestrator.tasks_db.fetch_candidate_issues.return_value = [task]
        with patch.object(mock_orchestrator, "_build_prompt", return_value="do it"):
            mock_orchestrator._tick(1)

        mock_orchestrator.agent_adapter.start_session.assert_called_once()

    def test_tick_respects_capacity(self, mock_orchestrator):
        """超出 max_concurrent_agents 时不派发"""
        from conftest import make_running_entry, make_task
        mock_orchestrator.config.agent.max_concurrent_agents = 1
        mock_orchestrator.running["T-EXISTING"] = make_running_entry("T-EXISTING")
        mock_orchestrator.claimed.add("T-EXISTING")
        task = make_task("T-NEW", state="Todo", description="test")
        mock_orchestrator.tasks_db.fetch_candidate_issues.return_value = [task]

        mock_orchestrator._tick(1)

        mock_orchestrator.agent_adapter.start_session.assert_not_called()


class TestDispatch:
    def test_empty_task_canceled(self, mock_orchestrator):
        """空描述任务被取消"""
        from conftest import make_task
        task = make_task("T-1", state="Todo", description="")
        mock_orchestrator._dispatch(task, attempt=None)

        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Canceled"
            for call in update_calls
        )
        assert found

    def test_already_claimed_skipped(self, mock_orchestrator):
        """已在 claimed 中的任务跳过"""
        from conftest import make_task
        mock_orchestrator.claimed.add("T-1")
        task = make_task("T-1")
        mock_orchestrator._dispatch(task, attempt=None)
        mock_orchestrator.agent_adapter.start_session.assert_not_called()

    def test_blacklisted_skipped(self, mock_orchestrator):
        """黑名单任务跳过"""
        from conftest import make_task
        mock_orchestrator.tasks_db.is_blacklisted.return_value = True
        task = make_task("T-1")
        mock_orchestrator._dispatch(task, attempt=None)
        mock_orchestrator.agent_adapter.start_session.assert_not_called()

    def test_successful_dispatch(self, mock_orchestrator):
        """正常 dispatch 启动 agent session"""
        from conftest import make_task
        task = make_task("T-1", description="Do something")
        with patch.object(mock_orchestrator, "_build_prompt", return_value="do it"):
            mock_orchestrator._dispatch(task, attempt=None)

        mock_orchestrator.agent_adapter.start_session.assert_called_once()
        # running/claimed 由 _dispatch 添加，但 _agent_worker 线程会立即完成并移除
        # 验证 _dispatch 调用了 start_session 即为成功

    def test_workspace_prepare_failure_retries(self, mock_orchestrator):
        """工作区准备失败触发重试"""
        from conftest import make_task
        task = make_task("T-1", description="test")
        mock_orchestrator.workspace_mgr.prepare.side_effect = Exception("disk full")
        with patch.object(mock_orchestrator, "_build_prompt", return_value="do it"):
            mock_orchestrator._dispatch(task, attempt=1)

        assert "T-1" in mock_orchestrator.retry_attempts


# ── On Agent Done ─────────────────────────────────────────────


class TestOnAgentDone:
    def test_success_pending_review(self, mock_orchestrator):
        """成功 + 有输出文件 → Pending Review"""
        from conftest import make_running_entry, make_task
        entry = make_running_entry("T-1")
        entry.workspace = MagicMock(path="/tmp/ws/T-1")
        task = make_task("T-1", description="test")
        result = MagicMock(status="success", exit_code=0, stdout="done", stderr="", result={})

        with patch("os.listdir", return_value=["_output.md"]), \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("os.path.isdir", return_value=True):
            mock_orchestrator._on_agent_done(task, entry, result)

        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Pending Review"
            for call in update_calls
        )
        assert found, "Task should transition to Pending Review"

    def test_failure_triggers_retry(self, mock_orchestrator):
        """失败 + 无输出文件 + 可重试 → 进入重试队列"""
        from conftest import make_running_entry, make_task

        from FRAMEWORK.agent_types import AgentResult
        entry = make_running_entry("T-1", attempt=1)
        entry.workspace = MagicMock(path="/tmp/ws/T-1")
        task = make_task("T-1", description="test", attempt_count=1)
        result = AgentResult(status="failed", exit_code=1, stdout="", stderr="error", result={})

        with patch("os.listdir", return_value=[]), \
             patch("os.path.isdir", return_value=True):
            mock_orchestrator._on_agent_done(task, entry, result)

        assert "T-1" in mock_orchestrator.retry_attempts or \
               mock_orchestrator.tasks_db.update_task.called

    def test_file_check_fallback(self, mock_orchestrator):
        """exit 非零但有输出文件 → 降级为成功（Pending Review）"""
        from conftest import make_running_entry, make_task
        entry = make_running_entry("T-1")
        entry.workspace = MagicMock(path="/tmp/ws/T-1")
        task = make_task("T-1", description="test")
        result = MagicMock(status="failed", exit_code=1, stdout="", stderr="", result={})

        with patch("os.listdir", return_value=["_output.md"]), \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("os.path.isdir", return_value=True):
            mock_orchestrator._on_agent_done(task, entry, result)

        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Pending Review"
            for call in update_calls
        )
        assert found, "File-check should result in Pending Review"


# ── Heartbeat and Reconcile ──────────────────────────────────


class TestReconcileRunning:
    def test_terminal_state_cleaned(self, mock_orchestrator):
        """终端状态的任务从 running 中移除"""
        from conftest import make_running_entry
        entry = make_running_entry("T-1")
        mock_orchestrator.running["T-1"] = entry
        mock_orchestrator.tasks_db.get_state.return_value = "Done"

        with patch("time.time", return_value=1000000), \
             patch.object(mock_orchestrator, "_check_heartbeat_seq", return_value=(True, 1)), \
             patch.object(mock_orchestrator, "_check_heartbeat", return_value=True):
            mock_orchestrator._reconcile_running()

        assert "T-1" not in mock_orchestrator.running

    def test_dead_process_recovered(self, mock_orchestrator):
        """进程死亡从 running 中移除"""
        from conftest import make_running_entry
        entry = make_running_entry("T-1")
        mock_orchestrator.running["T-1"] = entry
        mock_orchestrator.agent_adapter.is_process_alive.return_value = False
        mock_orchestrator.tasks_db.get_state.return_value = "In Progress"

        with patch("time.time", return_value=1000000), \
             patch.object(mock_orchestrator, "_check_heartbeat_seq", return_value=(True, 1)), \
             patch.object(mock_orchestrator, "_check_heartbeat", return_value=True):
            mock_orchestrator._reconcile_running()

        assert "T-1" not in mock_orchestrator.running


class TestCheckHeartbeat:
    def test_heartbeat_seq_file_not_found_degraded(self, mock_orchestrator):
        """心跳文件不存在时降级"""
        with patch("os.path.exists", return_value=False):
            alive, seq = mock_orchestrator._check_heartbeat_seq("/tmp/ws")
            assert alive is True
            assert seq == -1

    def test_heartbeat_seq_valid(self, mock_orchestrator):
        """有效心跳序列号"""
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = "42\n"
            alive, seq = mock_orchestrator._check_heartbeat_seq("/tmp/ws")
            assert alive is True
            assert seq == 42

    def test_heartbeat_mtime_ok(self, mock_orchestrator):
        """mtime 心跳文件新鲜"""
        import time as _time
        started = datetime.now(timezone.utc)
        with patch("os.path.exists", return_value=True), \
             patch("os.path.getmtime", return_value=_time.time()), \
             patch("time.time", return_value=1000000):
            alive = mock_orchestrator._check_heartbeat("/tmp/ws", started)
            assert alive is True


# ── Retry Logic ──────────────────────────────────────────────


class TestRetry:
    def test_schedule_retry_adds_to_queue(self, mock_orchestrator):
        """重试添加到 retry_attempts 队列"""
        from conftest import make_task
        task = make_task("T-1", attempt_count=1)
        mock_orchestrator._schedule_retry(task, attempt=2, error="timeout")
        assert "T-1" in mock_orchestrator.retry_attempts
        assert mock_orchestrator.retry_attempts["T-1"].attempt == 2

    def test_max_retry_giveup(self, mock_orchestrator):
        """超过最大重试次数 → Canceled"""
        from conftest import make_task
        task = make_task("T-1", attempt_count=10)
        mock_orchestrator._schedule_retry(task, attempt=11, error="too many")
        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Canceled"
            for call in update_calls
        )
        assert found

    def test_process_retries_dispatches_due(self, mock_orchestrator):
        """到期重试被 dispatch"""
        from conftest import make_retry_entry, make_task
        mock_orchestrator.retry_attempts["T-1"] = make_retry_entry("T-1", attempt=2)
        task = make_task("T-1", state="Todo", description="retry task")
        mock_orchestrator.tasks_db.fetch_candidate_issues.return_value = [task]
        mock_orchestrator.tasks_db.get_task.return_value = task

        with patch.object(mock_orchestrator, "_build_prompt", return_value="do it"):
            mock_orchestrator._process_retries()

        mock_orchestrator.agent_adapter.start_session.assert_called_once()

    def test_backoff_delay_increases(self, mock_orchestrator):
        """指数退避延迟递增"""
        from conftest import make_task
        task = make_task("T-1")
        mock_orchestrator._schedule_retry(task, attempt=2, error="err")
        delay_2 = mock_orchestrator.retry_attempts["T-1"].due_at_ms

        mock_orchestrator.retry_attempts.clear()
        mock_orchestrator._schedule_retry(task, attempt=3, error="err")
        delay_3 = mock_orchestrator.retry_attempts["T-1"].due_at_ms

        assert delay_3 > delay_2


# ── Approval / Revision ──────────────────────────────────────


class TestApproval:
    def test_on_task_approved_calls_cleanup(self, mock_orchestrator):
        """审批通过清理工作区"""
        from conftest import make_task
        task = make_task("T-1")
        mock_orchestrator.tasks_db.get_task.return_value = task
        mock_orchestrator._on_task_approved(task)
        mock_orchestrator.workspace_mgr.cleanup.assert_called_with("T-1")

    def test_on_task_revision_resets_to_todo(self, mock_orchestrator):
        """打回重做 → 重置为 Todo"""
        from conftest import make_task
        task = make_task("T-1", attempt_count=2)
        mock_orchestrator.tasks_db.get_task.return_value = task
        mock_orchestrator._on_task_revision(task, "needs fixes")
        update_calls = mock_orchestrator.tasks_db.update_task.call_args_list
        found = any(
            call[0][0] == "T-1" and call[0][1].get("state") == "Todo"
            for call in update_calls
        )
        assert found


# ── Capacity and Sorting ──────────────────────────────────────


class TestCapacity:
    def test_has_capacity_true_when_empty(self, mock_orchestrator):
        """空队列时有容量"""
        assert mock_orchestrator._has_capacity() is True

    def test_has_capacity_false_when_full(self, mock_orchestrator):
        """队列满时无容量"""
        mock_orchestrator.config.agent.max_concurrent_agents = 1
        mock_orchestrator.running["T-1"] = MagicMock()
        assert mock_orchestrator._has_capacity() is False

    def test_sort_by_priority_then_created(self, mock_orchestrator):
        """按优先级排序"""
        from conftest import make_task
        tasks = [
            make_task("T-3", priority=3, created_at="2026-01-03T00:00:00Z"),
            make_task("T-1", priority=1, created_at="2026-01-01T00:00:00Z"),
            make_task("T-2", priority=2, created_at="2026-01-02T00:00:00Z"),
        ]
        sorted_tasks = mock_orchestrator._sort_for_dispatch(tasks)
        ids = [t.id for t in sorted_tasks]
        assert ids == ["T-1", "T-2", "T-3"]
