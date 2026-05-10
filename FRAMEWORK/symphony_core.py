"""
Symphony-Lite 核心编排器
FRAMEWORK/symphony_core.py

职责:
- 维护内存状态(running / claimed / retry_attempts)
- 轮询 tick 循环
- 调度决策
- 对账
- 重试队列
- 重启恢复
"""
import atexit
import fcntl
import os
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_types import AgentResult, Session
    from config_loader import Config
    from file_watcher import FileWatcher
    from memory_manager import MemoryManager
    from notifier import Notifier
    from tasks_db import Task, TasksDB
    from workspace_mgr import Workspace, WorkspaceManager

try:
    from .logger import init_logger, log
except ImportError:
    from logger import init_logger, log  # script mode fallback


class RuntimeState(Enum):
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_QUEUED = "retry_queued"
    RELEASED = "released"


@dataclass
class RunningEntry:
    task_id: str
    identifier: str
    workspace: "Workspace"
    session: "Session | None"
    started_at: datetime
    last_event: str | None
    attempt: int
    grace_seconds: int = 60  # 心跳检测宽限期，防止 dispatch 后立即触发竞态


@dataclass
class RetryEntry:
    task_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    error: str | None


class SymphonyOrchestrator:
    """
    主编排器.

    状态机:
    - claimed: 正在调度但 worker 还未启动
    - running: worker 已启动,正在执行
    - retry_attempts: 失败重试队列
    """

    def __init__(self, config: "Config"):
        self.config = config
        self.workspace_mgr: "WorkspaceManager | None" = None
        self.agent_adapter = None  # 由 start() 中 AgentAdapter 子类赋值
        self.tasks_db: "TasksDB | None" = None
        self.memory_mgr: "MemoryManager | None" = None
        self.notifier: "Notifier | None" = None
        self.file_watcher: "FileWatcher | None" = None

        # 内存状态
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.instance_id: str = ""
        self._pending_review_ack: set[str] = set()  # 已通知过 Pending Review 的 task_id(避免重复通知)
        # 心跳序列号跟踪（I2：免疫时钟回拨）
        self._last_heartbeat_seq: dict[str, int] = {}  # task_id → 上次看到的序列号
        # I4：记忆系统竞态——主线程批量刷，避免并发写文件
        self._pending_memories: list = []  # [(task_id, title, topic, metadata, stdout, stderr, exit_code), ...]
        # I6：工作区低频清理计数器
        self._cleanup_tick_counter = 0
        # FIX-001：进程锁 fd（flock 文件描述符）
        self._instance_lock_fd: int | None = None

    def _acquire_instance_lock(self, lock_path: str) -> None:
        """
        获取独占实例锁，防止多实例同时运行。
        失败时直接退出（SystemExit），不抛异常。
        """
        lock_dir = os.path.dirname(lock_path)
        os.makedirs(lock_dir, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # 读取锁文件里的 PID，看谁在跑
            try:
                holder = open(lock_path, "r").read().strip()
            except Exception:
                holder = "(unknown)"
            os.close(fd)
            sys.stderr.write(
                f"[FATAL] Symphony-Lite instance already running (lock held by: {holder}).\n"
                f"If you believe this is wrong, remove: {lock_path}\n"
            )
            sys.exit(1)
        # 写入当前 PID 到锁文件，方便调试
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}".encode())
        os.fsync(fd)
        self._instance_lock_fd = fd
        # 注册退出时自动释放（进程退出时内核自动释放，这里显式做一次更干净）
        atexit.register(self._release_instance_lock)

    def _release_instance_lock(self) -> None:
        if self._instance_lock_fd is not None:
            try:
                fcntl.flock(self._instance_lock_fd, fcntl.LOCK_UN)
                os.close(self._instance_lock_fd)
            except Exception:
                pass
            self._instance_lock_fd = None

    def start(self) -> None:
        """启动主编排器"""
        # FIX-001：进程锁，在做任何初始化之前就要抢占
        # 锁文件放在 tasks.json 同目录下的隐藏文件
        import pathlib
        tasks_file = pathlib.Path(self.config.tracker.tasks_file)
        lock_file = tasks_file.parent / ".instance.lock"
        self._acquire_instance_lock(str(lock_file))

        global log
        log = init_logger(
            log_file=self.config.logging.file,
            level=self.config.logging.level,
            max_size_mb=self.config.logging.max_size_mb,
        )

        # 延迟导入子组件(避免循环依赖)
        from .file_watcher import FileWatcher
        from .memory_manager import MemoryManager
        from .notifier import Notifier
        from .tasks_db import TasksDB
        from .workspace_mgr import WorkspaceManager

        self.tasks_db = TasksDB(self.config.tracker.tasks_file)
        self.workspace_mgr = WorkspaceManager(self.config)

        # 动态解析 Agent 适配器（config.agent.executor 决定用哪个）
        import importlib
        executor = self.config.agent.executor
        if executor == "opencode":
            # OpenCode 适配器在 EXAMPLES/opencode_centric/ 中
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "EXAMPLES", "opencode_centric"))
            agent_module = importlib.import_module("agent_adapter")
            AgentImplClass = getattr(agent_module, "OpenCodeAgent")
        else:
            # 自定义适配器：假设它可在当前 PYTHONPATH 下被 import
            agent_module = importlib.import_module(f"{executor}_adapter")
            AgentImplClass = getattr(agent_module, f"{executor.capitalize()}Agent")
        self.agent_adapter = AgentImplClass(self.config)

        self.memory_mgr = MemoryManager(self.config)

        # 初始化通知器
        self.notifier = Notifier(self.config)
        self.notifier.on_approved(self._on_task_approved)
        self.notifier.on_revision(self._on_task_revision)
        self.notifier.start_listening(self.tasks_db)

        # 初始化文件监视器(监控 tasks.json 变化)
        self.file_watcher = FileWatcher(self.config.tracker.tasks_file, poll_interval=2.0)
        self.file_watcher.on_change(self._on_tasks_json_changed)
        self.file_watcher.start()

        log.info(event="startup_init", detail="Symphony-Lite starting...")
        self._startup_reconciliation()
        self._tick_loop()

    # ── 重启恢复 ───────────────────────────────────────────────────────────

    def _startup_reconciliation(self) -> None:
        """
        启动时对账:从 tasks.json 推断内存状态,不依赖任何持久化文件.

        逻辑:
        1. 扫描所有 state=In Progress 的任务
        2. 检查 lock_pid(PID 是否还在跑)
           - 在跑 -> 恢复 session 到 running[],重新加入监控
           - 不在 -> 判定为异常退出,打回 Todo,attempt++
        3. 重置 self.claimed = set()(内存丢了,必须重新 claim)
        """
        self.instance_id = f"{socket.gethostname()}-{os.getpid()}"
        log.info(
            event="reconciliation_start",
            detail=f"[Startup] instance_id={self.instance_id}, reconciling tasks.json...",
        )

        self.claimed = set()
        self.running = {}
        self.retry_attempts = {}

        all_tasks = self.tasks_db.load_all_tasks()

        for task in all_tasks:
            if task.state == "In Progress":
                if task.lock_pid is None:
                    log.warning(
                        event="reset_no_lockpid",
                        detail=f"[Startup] {task.id} is In Progress but no lock_pid, resetting to Todo",
                        task_id=task.id,
                    )
                    self.tasks_db.update_task(task.id, {
                        "state": "Todo",
                        "acquired_at": None,
                        "acquired_by": None,
                        "lock_pid": None,
                        "error": "Hermes crashed during execution, auto-reset on restart",
                    })
                    continue

                if self.agent_adapter.is_process_alive(task.lock_pid):
                    log.info(
                        event="recover_running",
                        detail=f"[Startup] {task.id} still running (pid={task.lock_pid}), recovering into running[]",
                        task_id=task.id,
                    )
                    workspace = self.workspace_mgr.prepare(task.id)
                    session = self.agent_adapter.__class__.__new__(type(self.agent_adapter))
                    session.id = f"{task.lock_pid}-1"
                    session.pid = task.lock_pid
                    session.workspace = workspace
                    session.process = None
                    self.running[task.id] = RunningEntry(
                        task_id=task.id,
                        identifier=task.id,
                        workspace=workspace,
                        session=session,
                        started_at=datetime.now(timezone.utc),
                        last_event="recovered_on_startup",
                        attempt=task.attempt_count,
                    )
                    self.claimed.add(task.id)
                    # I2：恢复时心跳序列号置 -1（下次 reconcile 时会立即更新为真实值）
                    self._last_heartbeat_seq[task.id] = -1
                else:
                    new_attempt = task.attempt_count  # FIX: 不递增——_dispatch 会做 +1，避免双倍增
                    log.warning(
                        event="reset_dead_pid",
                        detail=f"[Startup] {task.id} pid={task.lock_pid} dead, "
                               f"resetting to Todo (attempt {task.attempt_count} -> {new_attempt})",
                        task_id=task.id,
                    )
                    self.tasks_db.update_task(task.id, {
                        "state": "Todo",
                        "attempt_count": new_attempt,
                        "acquired_at": None,
                        "acquired_by": None,
                        "lock_pid": None,
                        "error": f"Agent crashed on restart (pid {task.lock_pid} not found)",
                    })

        # I5：启动对账完成后，扫描所有 is_retrying=True 的任务
        # 已到期：从 is_retrying 移除，重新加入候选池
        # 未到期：重建内存 retry 队列
        retrying_tasks = self.tasks_db.fetch_retrying_tasks()
        now = datetime.now(timezone.utc)
        for task in retrying_tasks:
            if task.retry_after:
                try:
                    due = datetime.fromisoformat(task.retry_after.replace("Z", "+00:00"))
                except ValueError:
                    due = now  # 解析失败，视为到期
                if due <= now:
                    self.tasks_db.update_retry_state(task.id, retry_after=None, is_retrying=False)
                    log.info(
                        event="retry_recovered",
                        detail=f"{task.id} retry window expired on restart, re-queuing",
                        task_id=task.id,
                    )
                else:
                    # 未到期：重建内存 retry 队列
                    due_at_ms = int(due.timestamp() * 1000)
                    self.retry_attempts[task.id] = RetryEntry(
                        task_id=task.id,
                        identifier=task.id,
                        attempt=task.attempt_count,
                        due_at_ms=due_at_ms,
                        error=task.error or "restart recovery",
                    )
                    log.info(
                        event="retry_recovered_pending",
                        detail=f"{task.id} retry pending until {task.retry_after}, restoring to queue",
                        task_id=task.id,
                    )

        log.info(
            event="reconciliation_done",
            detail=f"[Startup] Reconciliation done: {len(self.running)} running, "
                   f"{len(self.claimed)} claimed, {len(self.retry_attempts)} retry",
        )

    # ── 主循环 ─────────────────────────────────────────────────────────────

    def _tick_loop(self) -> None:
        """主循环:每 tick 执行一次对账 + 调度"""
        tick_count = 0
        while True:
            tick_count += 1
            try:
                self._tick(tick_count)
            except Exception as e:
                log.error(event="tick_error", detail=f"Tick #{tick_count} failed: {e}")
            time.sleep(self.config.polling.interval_ms / 1000)

    def _tick(self, tick_count: int) -> None:
        """
        每个 tick 的工作:
        1. 对账(停止不应该跑的)
        2. 检查重试计时器(到期就调度)
        3. 获取候选任务
        4. 排序 + 调度
        5. I4: 批量刷记忆文件（消灭并发写竞态）
        """
        self._reconcile_running()
        self._process_retries()

        candidates = self.tasks_db.fetch_candidate_issues(
            self.config.tracker.active_states
        )
        pending = [t for t in candidates if t.id not in self.claimed and t.id not in self.running]

        for task in self._sort_for_dispatch(pending):
            if not self._has_capacity():
                break
            self._dispatch(task, attempt=None)

        # I4：每个 tick 末尾批量刷记忆（主线程操作，消除并发写）
        self._flush_memories()

        # I6：低频工作区清理（Done/Canceled 超过 max_age_hours 自动删除）
        self._cleanup_tick_counter += 1
        cleanup_config = getattr(self.config.workspace, 'cleanup', None)
        if cleanup_config and cleanup_config.get('enabled', False):
            interval = cleanup_config.get('check_interval_ticks', 40)
            if self._cleanup_tick_counter >= interval:
                self._cleanup_tick_counter = 0
                self._cleanup_old_workspaces()

    def _flush_memories(self) -> None:
        """
        I4：主线程批量刷记忆文件，消灭并发写竞态。
        从 _pending_memories 队列取出所有待处理项，在主 tick 线程中顺序写入。
        """
        if not self._pending_memories:
            return
        memories = self._pending_memories
        self._pending_memories = []
        for item in memories:
            if len(item) == 4:
                # 旧格式（直接传 Task 对象，兼容）
                task, stdout, stderr, exit_code = item
            else:
                # 新格式：元组展平（task_id, title, topic, metadata, stdout, stderr, exit_code）
                task_id, title, topic, metadata, stdout, stderr, exit_code = item
                task = self.tasks_db.get_task(task_id)
                if not task:
                    continue
            try:
                self.memory_mgr.on_task_complete(task, stdout, stderr, exit_code)
            except Exception as e:
                log.warning(
                    event="memory_flush_failed",
                    detail=f"Memory flush failed for {task.id}: {e}",
                    task_id=task.id,
                )

    # ── I7: 记忆上下文注入 ──────────────────────────────────────────────

    def _build_memory_context(self, task: "Task") -> str:
        """
        根据 task.topic 从记忆索引中构建过滤后上下文片段，
        注入到 _build_prompt 的描述之后。
        keywords 从 task.metadata["keywords"] 读取（tasks.json 中声明）。
        """
        if not self.memory_mgr:
            return ""
        topic = getattr(task, "topic", None) or ""
        if not topic:
            return ""
        keywords = []
        if task.metadata:
            keywords = task.metadata.get("keywords", [])
        try:
            return self.memory_mgr.build_filtered_context(topic, keywords)
        except Exception as e:
            log.warning(
                event="memory_context_failed",
                detail=f"Failed to build memory context for {task.id}: {e}",
                task_id=task.id,
            )
            return ""

    # ── Prompt 构建 ────────────────────────────────────────────────────────
    # opencode --agent 不理解的系统文件（在 workspace_has_output 中排除）
    _OUTPUT_SYSTEM_FILES = frozenset({
        "prompt.txt", "result.json", ".heartbeat", ".done",
        "opencode_wrapper.sh", "opencode.log",
    })

    def _build_prompt(self, task: "Task") -> str:
        """

        构建发送给 opencode --agent 的 prompt。

        关键设计修正（FIX: empty-output）：
        - 移除 STRUCTURED_OUTPUT JSON 协议（opencode 是 TUI agent，不解析这个格式）
        - 替换为强制落盘指令：所有输出必须写文件，不能只回复到终端
        """
        # ── 防护3: 空任务占位 prompt，防止 opencode 收到空白指令空转 ──
        if not task.title.strip() and not task.description.strip():
            return (
                f"## 任务 ID: {task.id}\n"
                f"## 标题: (空任务)\n"
                "此任务描述为空。立即退出，不做任何操作。\n"
                "在当前目录写入一个空文件 EMPTY_TASK.md 表示完成。\n"
            )

        lines = []
        lines.append(f"## 任务 ID: {task.id}\n")
        lines.append(f"## 标题: {task.title}\n")
        lines.append(f"## 描述\n\n{task.description}\n")

        # I7：注入记忆上下文（滑动窗口 + 关键词过滤）
        mem_ctx = self._build_memory_context(task)
        if mem_ctx:
            lines.append("\n### 上下文记忆\n" + mem_ctx + "\n")

        # ── 执行约束 ──────────────────────────────────────────────────────────
        lines.append("\n## 执行约束\n")
        lines.append("- 当前工作目录即为 workspace，所有操作在此目录下完成\n")
        lines.append("- 不要 cd 到外部路径\n")
        lines.append("- 可以读/写/修改此目录下的任何文件\n")

        # ── 强制输出指令（FIX: empty-output 核心）─────────────────────────────
        lines.append("\n## 强制输出要求（必须遵守，否则任务视为失败）\n")
        lines.append("")
        lines.append("你必须将执行结果写入当前目录的文件中。仅回复文字到终端不算完成任务。\n")
        lines.append("")
        lines.append("根据任务类型，选择对应方式：\n")
        lines.append("")
        lines.append("1. **调研/分析/报告类任务**：将完整报告写入文件 `_output.md`\n")
        lines.append("   - 文件至少 1000 字节，使用 Markdown 格式\n")
        lines.append("   - 每个章节有技术细节支撑\n")
        lines.append("")
        lines.append("2. **代码修改/重构类任务**：\n")
        lines.append("   a) 直接编辑目标文件（修改代码）\n")
        lines.append("   b) 在 `_changes.md` 中记录做了哪些修改、改了什么文件、改了什么内容\n")
        lines.append("")
        lines.append("3. **代码生成类任务**：将生成的代码写入对应文件名，并在 `_output.md` 中说明用途\n")
        lines.append("")
        lines.append("4. **其他类型任务**：将结果写入 `_output.md`，至少 500 字节\n")
        lines.append("")
        lines.append("### 完成确认\n")
        lines.append("执行完毕后，运行 `ls -la` 确认文件已生成。\n")

        return "".join(lines)

    def _dispatch(self, task: "Task", attempt: int | None) -> None:
        """
        调度一个任务(非阻塞):
        1. 标记 claimed(写 acquired_at/acquired_by/lock_pid 到 tasks.json)
        2. 准备工作区
        3. 启动 worker 线程
        """
        # ── 防护1: 空任务直接取消，不重复派发 ──────────────────────────
        if not task.description.strip():
            log.warning(event="empty_task_canceled", task_id=task.id, detail="Task description is empty, canceling")
            self.tasks_db.update_task(task.id, {
                "state": "Canceled",
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "result": {"error": "empty task description"},
            })
            self.claimed.discard(task.id)
            return

        # ── 防护2: 已在 claimed 集合中的任务不再重复派发 ──────────────
        if task.id in self.claimed:
            log.info(event="already_claimed_skip", task_id=task.id,
                     detail="Task in claimed set, skipping duplicate dispatch")
            return

        # ── FIX-003: 黑名单检查，防止被放弃的任务被重新调度 ─────────────
        if self.tasks_db.is_blacklisted(task.id):
            log.warning(event="blacklisted_skip", task_id=task.id, detail="Task is blacklisted, skipping dispatch")
            self.claimed.discard(task.id)
            return

        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        actual_attempt = attempt if attempt is not None else task.attempt_count + 1

        # 先加 claimed 集合（防并发重入），再写 DB
        self.claimed.add(task.id)
        self.tasks_db.update_task(task.id, {
            "state": "In Progress",
            "acquired_at": now_iso,
            "acquired_by": self.instance_id,
            "lock_pid": None,
            "updated_at": now_iso,
        })

        # FIX-004: WorkspaceOccupiedError → 跳过（workspace 被其他进程占用，不 retry）
        from workspace_mgr import WorkspaceOccupiedError
        try:
            workspace = self.workspace_mgr.prepare(task.id)
        except WorkspaceOccupiedError as e:
            log.warning(
                event="workspace_occupied_skip",
                detail=f"Workspace occupied for {task.id}: {e}, skipping this tick",
                task_id=task.id,
            )
            self.claimed.discard(task.id)
            return
        except Exception as e:
            log.error(event="workspace_prepare_failed",
                     detail=f"Workspace prepare failed for {task.id}: {e}", task_id=task.id)
            self.tasks_db.update_task(task.id, {
                "state": "Todo",
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
            })
            self.claimed.discard(task.id)
            self._schedule_retry(task, attempt=actual_attempt + 1, error=str(e))
            return

        # ── Phase 1: External File Sandbox — 注入外部文件到 workspace ───────
        # opencode 在 workspace 副本上工作，全程不知道自己在操作"副本"
        self.workspace_mgr._prepare_workspace(workspace.path, task.id, task.metadata)

        # before_run 钩子
        try:
            self.workspace_mgr.run_hook("before_run", workspace.path, task.id)
        except Exception as e:
            log.error(event="before_run_failed", detail=f"before_run hook failed for {task.id}: {e}", task_id=task.id)
            self.tasks_db.update_task(task.id, {
                "state": "Todo",
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
            })
            self.claimed.discard(task.id)
            self._schedule_retry(task, attempt=actual_attempt + 1, error=str(e))
            return

        # 启动 Agent session
        try:
            prompt = self._build_prompt(task)
            session = self.agent_adapter.start_session(workspace, prompt)
        except Exception as e:
            log.error(event="agent_start_failed", detail=f"Agent start failed for {task.id}: {e}", task_id=task.id)
            self.tasks_db.update_task(task.id, {
                "state": "Todo",
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
            })
            self.claimed.discard(task.id)
            self._schedule_retry(task, attempt=actual_attempt + 1, error=str(e))
            return

        # 补充 lock_pid
        self.tasks_db.update_task(task.id, {
            "lock_pid": session.pid,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })

        entry = RunningEntry(
            task_id=task.id,
            identifier=task.id,
            workspace=workspace,
            session=session,
            started_at=datetime.now(timezone.utc),
            last_event="session_started",
            attempt=actual_attempt,
        )
        self.running[task.id] = entry
        # I2：初始化心跳序列号记录（-1 表示"尚未见过"）
        self._last_heartbeat_seq[task.id] = -1

        # 启动 worker 线程(非阻塞)
        thread = threading.Thread(
            target=self._agent_worker,
            args=(task, entry),
            name=f"agent-worker-{task.id}",
        )
        thread.daemon = True
        thread.start()

        log.info(
            event="dispatched",
            detail=f"Dispatched {task.id} to workspace {workspace.path}",
            task_id=task.id,
            session_id=session.id,
        )

    def _agent_worker(self, task: "Task", entry: RunningEntry) -> None:
        """Agent worker 线程:等 agent 跑完,然后回调"""
        try:
            result = self.agent_adapter.wait_session(entry.session)
            self._on_agent_done(task, entry, result)
        except Exception as e:
            log.error(event="worker_exception", detail=f"Agent worker exception for {task.id}: {e}", task_id=task.id)
            from agent_adapter import AgentResult
            self._on_agent_done(task, entry, AgentResult(
                status="failed",
                exit_code=-1,
                stdout="",
                stderr=str(e),
                result={},
            ))

    def _on_agent_done(self, task: "Task", entry: RunningEntry, result: "AgentResult") -> None:
        """
        Agent 结束后统一回调:
        - 成功 -> Pending Review,触发记忆生成
        - 失败 -> retry 调度(或超过最大次数则放弃)
        """
        # 1. 跑 after_run 钩子
        try:
            self.workspace_mgr.run_hook("after_run", entry.workspace.path, task.id)
        except Exception as e:
            log.warning(event="after_run_failed", detail=f"after_run hook failed for {task.id}: {e}", task_id=task.id)

        # 2. success 时:把 output_file 从 workspace 搬移到 inbox/
        if result.status == "success" and task.metadata and task.metadata.get("output_file"):
            src = os.path.join(entry.workspace.path, task.metadata["output_file"])
            dst_dir = self.config.notification.inbox_dir
            dst = os.path.join(dst_dir, task.metadata["output_file"])
            if os.path.exists(src):
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(src, dst)
                log.info(
                    event="artifact_published",
                    detail=f"Published {task.metadata['output_file']} -> {dst}",
                    task_id=task.id,
                )
            else:
                log.warning(
                    event="artifact_not_found",
                    detail=f"Output file {src} not found, skipping publish",
                    task_id=task.id,
                )

        # ── 文件完整性检测（两种情况都要查）───────────────────────────────
        # FIX: 不再限定 .md > 500 字节，检查任何非系统文件。
        # 代码修改类任务产出是 .py / .yaml 等，不会被旧逻辑捕获。
        workspace_has_output = False
        ws_path = entry.workspace.path
        if os.path.isdir(ws_path):
            for fname in os.listdir(ws_path):
                if fname in self._OUTPUT_SYSTEM_FILES:
                    continue
                fpath = os.path.join(ws_path, fname)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                    workspace_has_output = True
                    break

        if result.status == "success" and workspace_has_output:
            self.tasks_db.update_task(task.id, {
                "state": "Pending Review",
                "result": {"stdout": result.stdout[-500:] if result.stdout else ""},
            })
            # 写记忆（I4：改为主线程批量刷，避免并发写竞态）
            self._pending_memories.append((
                task.id, task.title, task.topic, task.metadata,
                result.stdout, result.stderr, result.exit_code,
            ))
            # 通知 GRaffe(去重)
            if task.id not in self._pending_review_ack:
                self._pending_review_ack.add(task.id)
                if self.notifier:
                    self.notifier.notify_task_pending_review(task)
            log.info(
                event="task_success",
                detail=f"{task.id} completed successfully, state -> Pending Review",
                task_id=task.id,
            )

            # ── Phase 3: auto-verify ───────────────────────────────────────────
            verify_after_approve = task.metadata and task.metadata.get("verify_after_approve", False)
            if verify_after_approve and getattr(self.config, "verify", None) and self.config.verify.auto_enabled:
                self._auto_verify_and_approve(task, entry, result)
        else:
            # ── 失败/无文件路径：重试调度 ─────────────────────────────────
            if workspace_has_output:
                # exit 非零但文件存在——file-check 降级为成功
                log.warning(
                    event="task_success_via_file_check",
                    detail=f"{task.id} had output files despite exit "
                           f"{result.exit_code}, treating as success",
                    task_id=task.id,
                )
                self.tasks_db.update_task(task.id, {
                    "state": "Pending Review",
                    "result": {"stdout": result.stdout[-500:] if result.stdout else ""},
                })
                self._pending_memories.append((
                    task.id, task.title, task.topic, task.metadata,
                    result.stdout, result.stderr, result.exit_code,
                ))
                if task.id not in self._pending_review_ack:
                    self._pending_review_ack.add(task.id)
                    if self.notifier:
                        self.notifier.notify_task_pending_review(task)
                log.info(
                    event="task_success",
                    detail=f"{task.id} completed (file-check fallback), state -> Pending Review",
                    task_id=task.id,
                )
            elif entry.attempt >= 10:
                log.error(
                    event="max_retries_exceeded",
                    detail=f"{task.id} exceeded max retries (attempt {entry.attempt}), giving up",
                    task_id=task.id,
                )
                self.tasks_db.update_task(task.id, {
                    "state": "Canceled",
                    "acquired_at": None,
                    "acquired_by": None,
                    "lock_pid": None,
                    "error": f"Max retries exceeded: {result.stderr or result.exit_code}",
                })
                self.claimed.discard(task.id)
            else:
                self._schedule_retry(
                    task,
                    attempt=entry.attempt + 1,
                    error=f"exit_{result.exit_code}: {result.stderr or 'no output files'}",
                )

        # 清理锁(不删 workspace,等 GRaffe 验收)
        self._terminate(task.id, cleanup=False)
        self.running.pop(task.id, None)

    # ── 对账 ────────────────────────────────────────────────────────────────

    def _reconcile_running(self) -> None:
        """
        对账(每个 tick):
        1. 心跳停滞检测
        2. 外部状态检查(terminal)
        3. 进程存活抽检
        """
        to_remove = []

        for task_id, entry in list(self.running.items()):
            # Grace period: newly dispatched tasks get N seconds before heartbeat check kicks in.
            # Also skip if the heartbeat file predates this dispatch/recovery — it's a
            # stale file from a previous symphony instance, not a real stall.
            elapsed_since_start_ms = (time.time() - entry.started_at.timestamp()) * 1000
            in_grace = elapsed_since_start_ms < (entry.grace_seconds * 1000)

            # 双保险心跳检测：先查序列号（主判断），再查 mtime（降级兜底）
            # I2：免疫时钟回拨——序列号不增长即判定为卡死
            seq_ok, last_seq = self._check_heartbeat_seq(entry.workspace.path)
            prev_seq = self._last_heartbeat_seq.get(task_id, -1)
            seq_stale = seq_ok and last_seq <= prev_seq  # 序列号没增长说明卡死

            mtime_ok = self._check_heartbeat(entry.workspace.path, entry.started_at)

            if not in_grace and (seq_stale or not mtime_ok):
                # 更新记录（即使要 kill 也记录当前序列号）
                self._last_heartbeat_seq[task_id] = last_seq
                log.warning(
                    event="heartbeat_stall",
                    detail=f"Heartbeat stall for {task_id} (seq_stale={seq_stale}, mtime_ok={mtime_ok}), terminating",
                    task_id=task_id,
                )
                try:
                    self._terminate(task_id, cleanup=True)
                except Exception as e:
                    log.error(event="terminate_failed", detail=f"Terminate failed for {task_id}: {e}", task_id=task_id)
                self._schedule_retry_from_entry(entry, error="heartbeat stall")
                to_remove.append(task_id)
                continue

            # 2. 外部状态检查
            tracker_state = self.tasks_db.get_state(task_id)
            if tracker_state in self.config.tracker.terminal_states:
                log.info(
                    event="terminal_detected",
                    detail=f"{task_id} is terminal ({tracker_state}), cleaning up",
                    task_id=task_id,
                )
                try:
                    self._terminate(task_id, cleanup=True)
                except Exception as e:
                    log.error(event="terminate_failed", detail=f"Terminate failed for {task_id}: {e}", task_id=task_id)
                to_remove.append(task_id)
                continue

            # 3. 进程存活抽检
            if entry.session and entry.session.pid:
                if not self.agent_adapter.is_process_alive(entry.session.pid):
                    log.warning(
                        event="process_dead",
                        detail=f"Process {entry.session.pid} for {task_id} is dead, terminating",
                        task_id=task_id,
                    )
                    try:
                        self._terminate(task_id, cleanup=True)
                    except Exception as e:
                        log.error(event="terminate_failed",
                                 detail=f"Terminate failed for {task_id}: {e}", task_id=task_id)
                    self._schedule_retry_from_entry(entry, error=f"process died (pid {entry.session.pid})")
                    to_remove.append(task_id)

        for task_id in to_remove:
            self.running.pop(task_id, None)
            # I2：清理心跳序列号记录
            self._last_heartbeat_seq.pop(task_id, None)

    def _check_heartbeat_seq(self, workspace_path: str) -> tuple[bool, int]:
        """
        心跳序列号检测——免疫时钟回拨。
        返回 (is_alive, last_seq)：
        - 文件不存在 → (True, -1)（降级到 mtime 判断）
        - 序列号无法解析 → (True, -1)
        - 序列号 <= 上次记录 → (False, seq)（证明没有增长，卡死）
        - 序列号 > 上次记录 → (True, seq)（正常增长，存活）
        """
        heartbeat_file = os.path.join(workspace_path, ".heartbeat")
        if not os.path.exists(heartbeat_file):
            return True, -1  # 降级

        try:
            with open(heartbeat_file) as f:
                content = f.read().strip()
            parts = content.split()
            if len(parts) < 1:
                return True, -1
            seq = int(parts[0])
            return True, seq
        except (ValueError, OSError):
            return True, -1  # 解析失败，降级

    def _check_heartbeat(self, workspace_path: str, started_at: datetime = None) -> bool:
        """
        检查心跳文件:返回 True=存活,False=停滞(需 kill).
        - 如果 .heartbeat 不存在,默认存活(依赖 stall_timeout 兜底)
        - 如果心跳文件 mtime 早于 started_at,说明是前一个实例的残留,视为存活
        - mtime 距离当前 > stall_timeout_ms -> 停滞
        """
        heartbeat_file = os.path.join(workspace_path, ".heartbeat")
        if not os.path.exists(heartbeat_file):
            return True

        mtime_sec = os.path.getmtime(heartbeat_file)
        # 残留文件：心跳文件比本次启动还旧，说明是前一个实例的
        if started_at is not None and mtime_sec < started_at.timestamp():
            return True
        elapsed_ms = (time.time() - mtime_sec) * 1000
        return elapsed_ms < self.config.timeouts.stall_timeout_ms

    def _terminate(self, task_id: str, cleanup: bool) -> None:
        """停止 Agent + 可选清理工作区"""
        entry = self.running.get(task_id)
        if entry and entry.session:
            self.agent_adapter.stop_session(entry.session)
        if cleanup:
            self.workspace_mgr.cleanup(task_id)
        # 解锁:清除锁字段
        self.tasks_db.update_task(task_id, {
            "acquired_at": None,
            "acquired_by": None,
            "lock_pid": None,
        })
        self.claimed.discard(task_id)

    def _cleanup_old_workspaces(self) -> None:
        """
        I6：低频清理——删除 Done/Canceled 超过 max_age_hours 的 workspace。
        避免磁盘耗尽，同时保留最近的任务工作区供 GRaffe 排查。
        """
        if not self.workspace_mgr:
            return
        cleanup_config = self.config.workspace.cleanup
        max_age_hours = cleanup_config.get('max_age_hours', 24)
        cutoff_ts = time.time() - max_age_hours * 3600

        all_tasks = self.tasks_db.load_all_tasks()
        done_tasks = [t for t in all_tasks
                      if t.state in self.config.tracker.terminal_states]

        cleaned = 0
        for task in done_tasks:
            ws_path = self.workspace_mgr.config.get_workspace_path(task.id)
            if not os.path.exists(ws_path):
                continue
            # 检查 workspace mtime
            try:
                ws_mtime = os.path.getmtime(ws_path)
            except OSError:
                continue
            if ws_mtime < cutoff_ts:
                log.info(
                    event="workspace_cleanup",
                    detail=f"Cleaning up workspace for {task.id} (mtime age > {max_age_hours}h)",
                    task_id=task.id,
                )
                self.workspace_mgr.cleanup(task.id)
                cleaned += 1
        if cleaned > 0:
            log.info(event="workspace_cleanup_batch", detail=f"Cleaned {cleaned} old workspaces")

    # ── 重试 ───────────────────────────────────────────────────────────────

    def _schedule_retry_from_entry(self, entry: RunningEntry, error: str) -> None:
        """从 RunningEntry 发起重试"""
        task = self.tasks_db.get_task(entry.task_id)
        if not task:
            log.warning(event="retry_task_not_found", detail=f"Cannot retry {entry.task_id}: task not found")
            return
        self._schedule_retry(task, attempt=entry.attempt + 1, error=error)

    def _schedule_retry(self, task: "Task", attempt: int, error: str) -> None:
        """指数退避重试（I5：退避时间持久化到 tasks.json，重启后可恢复）"""
        if attempt > 10:
            log.error(event="max_retry_giveup",
                     detail=f"Max retries exceeded for {task.id}, giving up", task_id=task.id)
            self.claimed.discard(task.id)
            # FIX-003：从 retry_attempts 内存队列中删除，并写黑名单持久化
            self.retry_attempts.pop(task.id, None)
            self.tasks_db.update_task(task.id, {
                "state": "Canceled",
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
                "error": f"Max retries exceeded: {error}",
            })
            self.tasks_db.blacklist_task(task.id)
            return

        delay_ms = min(10_000 * (2 ** (attempt - 1)), self.config.agent.max_retry_backoff_ms)
        due_at_ms = int(time.time() * 1000) + delay_ms

        # I5：计算退避到期时间，写入 tasks.json 持久化
        from datetime import timedelta
        due_at = datetime.now(timezone.utc) + timedelta(milliseconds=delay_ms)
        due_at_iso = due_at.isoformat().replace("+00:00", "Z")

        self.tasks_db.update_task(task.id, {
            "attempt_count": attempt,
            "error": error,
            "retry_after": due_at_iso,   # I5 新增：持久化退避截止时间
            "is_retrying": True,          # I5 新增：标记为退避中
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        self.retry_attempts[task.id] = RetryEntry(
            task_id=task.id,
            identifier=task.id,
            attempt=attempt,
            due_at_ms=due_at_ms,
            error=error,
        )
        log.info(
            event="retry_scheduled",
            detail=f"Scheduled retry for {task.id} in {delay_ms}ms (attempt {attempt}), due_at={due_at_iso}",
            task_id=task.id,
        )

    def _process_retries(self) -> None:
        """检查到期的重试条目,触发重新调度"""
        now_ms = int(time.time() * 1000)
        for task_id, entry in list(self.retry_attempts.items()):
            if entry.due_at_ms <= now_ms:
                self.retry_attempts.pop(task_id)
                candidates = self.tasks_db.fetch_candidate_issues(
                    self.config.tracker.active_states
                )
                task = next((t for t in candidates if t.id == task_id), None)
                if task:
                    # I5：调度前清除 is_retrying 持久化状态
                    self.tasks_db.update_retry_state(task_id, retry_after=None, is_retrying=False)
                    self._dispatch(task, attempt=entry.attempt)
                else:
                    self.claimed.discard(task_id)

    # ── 任务审批回调 ────────────────────────────────────────────────────

    def _on_task_approved(self, task: "Task") -> None:
        """
        审批通过回调(Pending Review -> Done):
        - Phase 2: 把 workspace 中的 external_files 写回 original 路径
        - 清理工作区(before_remove 钩子)
        - 通知结束
        """
        task_obj = self.tasks_db.get_task(task.id)
        if not task_obj:
            return

        # ── Phase 2: External File Sandbox — 写回修改 ───────────────────────
        workspace_path = self.config.get_workspace_path(task.id)
        self.workspace_mgr.deploy_sandboxed_files(
            workspace_path=workspace_path,
            task_id=task.id,
            metadata=task_obj.metadata,
            config=self.config,
        )

        log.info(
            event="task_approved",
            detail=f"GRaffe approved {task.id}, deployed sandboxed files and cleaning up workspace",
            task_id=task.id,
        )
        self.workspace_mgr.cleanup(task.id)
        # 从 pending_review_ack 中移除
        self._pending_review_ack.discard(task.id)

    def _on_task_revision(self, task: "Task", error: str) -> None:
        """
        GRaffe 打回重做回调(Pending Review -> Todo):
        - 累加 attempt_count
        - 重新调度
        - 清理工作区(GRaffe 可能会复用,但先清)
        """
        task_obj = self.tasks_db.get_task(task.id)
        if not task_obj:
            return

        new_attempt = task_obj.attempt_count + 1
        log.info(
            event="task_revision",
            detail=f"Revision requested for {task.id} "
                   f"(attempt {task_obj.attempt_count} -> {new_attempt}): {error}",
            task_id=task.id,
        )

        # 打回 Todo,累加 attempt
        self.tasks_db.update_task(task.id, {
            "state": "Todo",
            "attempt_count": new_attempt,
            "acquired_at": None,
            "acquired_by": None,
            "lock_pid": None,
            "error": f"GRaffe revision: {error}" if error else "GRaffe requested revision",
        })
        # I5：清除退避状态（GRaffe 打回后重新走重试逻辑）
        self.tasks_db.update_retry_state(task.id, retry_after=None, is_retrying=False)
        self._pending_review_ack.discard(task.id)
        self.claimed.discard(task.id)
        self.running.pop(task.id, None)

        # 清理工作区(重新来)
        # I6：先归档本次尝试结果（供 GRaffe 排查），再清理 workspace
        if self.workspace_mgr:
            self.workspace_mgr.archive_attempt(task.id, task_obj.attempt_count)
        self.workspace_mgr.cleanup(task.id)

    def _auto_verify_and_approve(self, task: "Task", entry: "RunningEntry", result: "AgentResult") -> None:
        """
        Phase 3: auto-verify 回调——Pending Review 后自动调用 momment 评分。

        - ≥阈值：自动 approve，写回文件，状态→ Done
        - <阈值：自动 revise，状态→ Todo，workspace 清理
        - 异常：静默跳过，保持 Pending Review（GRaffe 手动验收）
        """
        try:
            from momment_module.scorer import score as _momment_score
        except Exception as e:
            log.warning(
                event="auto_verify_skipped",
                detail=f"Could not import momment scorer: {e}",
                task_id=task.id,
            )
            return

        try:
            score_result = _momment_score(
                task=task.description,
                response=result.stdout or "",
                agent_method="opencode",
            )
        except Exception as e:
            log.warning(
                event="auto_verify_error",
                detail=f"momment scoring failed: {e}",
                task_id=task.id,
            )
            return

        threshold = self.config.verify.threshold
        passed = score_result.total_score >= threshold

        log.info(
            event="auto_verify_done",
            detail=f"momment score={score_result.total_score} (threshold={threshold}), passed={passed}",
            task_id=task.id,
        )

        if passed:
            # 自动 approve → 写回文件 + Done
            try:
                self.workspace_mgr.deploy_sandboxed_files(
                    workspace_path=entry.workspace.path,
                    task_id=task.id,
                    metadata=task.metadata,
                    config=self.config,
                )
            except Exception as e:
                log.error(event="auto_deploy_failed", detail=str(e), task_id=task.id)

            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self.tasks_db.update_task(task.id, {
                "state": "Done",
                "updated_at": now_iso,
            })
            self.workspace_mgr.cleanup(task.id)
            self._pending_review_ack.discard(task.id)
            log.info(event="auto_approved",
                     detail=f"{task.id} auto-approved (score={score_result.total_score})", task_id=task.id)
        else:
            # 自动 revise → 重置为 Todo
            new_attempt = task.attempt_count + 1
            self.tasks_db.update_task(task.id, {
                "state": "Todo",
                "attempt_count": new_attempt,
                "acquired_at": None,
                "acquired_by": None,
                "lock_pid": None,
                "error": f"Auto-verify failed: score={score_result.total_score} < {threshold}",
            })
            self.tasks_db.update_retry_state(task.id, retry_after=None, is_retrying=False)
            self._pending_review_ack.discard(task.id)
            self.claimed.discard(task.id)
            self.running.pop(task.id, None)

            if self.workspace_mgr:
                self.workspace_mgr.archive_attempt(task.id, task.attempt_count)
            self.workspace_mgr.cleanup(task.id)
            log.info(
                event="auto_revised",
                detail=f"{task.id} auto-revised (score={score_result.total_score} < {threshold})",
                task_id=task.id,
            )

    def _on_tasks_json_changed(self, path: str) -> None:
        """
        tasks.json 被外部修改时的回调(由 FileWatcher 触发).
        当前 tick 循环已经会处理状态变化,这里只打日志.
        同时异步通知 sillack-web 的监控页面(毫秒级实时).
        """
        log.debug(event="tasks_json_changed", detail="tasks.json changed externally, will reconcile on next tick")

        # 异步 POST 到 sillack-web，立即触发 SSE 广播（不阻塞 FileWatcher）
        try:
            import json as _json
            import threading
            import urllib.request

            def _do_webhook():
                try:
                    url = f"{getattr(self.config, 'sillack_web_url', None) or 'http://localhost:8001'}/api/symphony/webhook"
                    body = _json.dumps(
                        {"event": "tasks_json_changed",
                         "detail": "file watcher triggered"}
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=5):
                        pass
                except Exception:
                    pass  # 不影响主流程

            t = threading.Thread(target=_do_webhook, daemon=True, name="symphony-webhook")
            t.start()
        except Exception:
            pass

    # ── 辅助 ────────────────────────────────────────────────────────────────

    def _has_capacity(self) -> bool:
        """是否有可用并发槽位"""
        return len(self.running) < self.config.agent.max_concurrent_agents

    def _sort_for_dispatch(self, tasks: list) -> list:
        """
        排序规则:
        1. priority 升序(数字越小优先级越高)
        2. created_at 最旧优先
        3. id 字典序决胜
        """
        return sorted(
            tasks,
            key=lambda t: (t.priority or 999, t.created_at, t.id)
        )
