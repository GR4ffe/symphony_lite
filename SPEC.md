# Symphony-Lite: Agent Orchestration Framework Specification

> A lightweight, state-driven framework for orchestrating multiple AI coding agents in coordinated workflows. Designed for autonomous task execution with human-in-the-loop approval.

---

## 1. Background and Motivation

### The Problem

Single-agent systems hit hard ceilings on complex, multi-step tasks. A lone agent must simultaneously understand context, plan steps, execute tools, manage memory, and handle errors—resulting in context overload, reliability issues, and the inability to delegate.

### The Observation

Coordination—not capability—is the bottleneck. The most powerful AI coding agents (OpenCode, Claude Code, Codex) are already capable. What they lack is a **structured environment** that:

- Breaks work into discrete, verifiable tasks
- Maintains persistent state across agent restarts
- Isolates agent mutations until human approval
- Provides memory across task boundaries
- Handles permission prompts automatically

### The Approach

Symphony-Lite applies the **Supervisor-Worker** pattern from multi-agent systems to autonomous task execution. A lightweight orchestrator ("Supervisor") manages task lifecycle, delegates to pluggable agent runtimes ("Workers"), and enforces safety boundaries. The result is a system that can run unattended while retaining human oversight at every critical decision point.

---

## 2. Design Philosophy

### Principle 1: Separation of Concerns

The Supervisor never does the actual coding work. It orchestrates: deciding what runs next, feeding context to workers, monitoring progress, and enforcing state transitions. Workers (OpenCode, Claude Code, etc.) focus purely on execution.

### Principle 2: State is the Source of Truth

All task state lives in a single `tasks.json` file. The Supervisor's in-memory state is derived from this file at startup (reconciliation), making restarts safe. No task is ever lost; crashes are recovered, not discarded.

### Principle 3: Sandbox-and-Approve

Workers never modify production files directly. They work in isolated workspace directories. All changes are reviewed (manually or automatically) before being written back. This makes the system inherently safe for unattended operation.

### Principle 4: Pluggable Agent Runtimes

The Worker layer is abstracted behind an `AgentAdapter` interface. OpenCode is one possible adapter. Others (Claude Code, Codex, custom agents) plug in without touching the Supervisor or state machine.

### Principle 5: Explicit Retry with Bounded Attempts

Failures are not silent. Exponential backoff guides retries, and a hard cap (default: 10 attempts) prevents infinite loops. Each attempt is logged and recoverable.

---

## 3. Architecture

### 3.1 System Topology

```
┌─────────────────────────────────────────────────────────┐
│                    tasks.json                           │
│         (single source of truth, file-based)           │
└──────────────────────┬──────────────────────────────────┘
                       │ poll / watch
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  SymphonyOrchestrator                   │
│                   (Supervisor / Tick Loop)             │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Memory state: claimed{}, running{}, retries{}  │  │
│  └──────────────────────────────────────────────────┘  │
│         │              │               │              │
│         ▼              ▼               ▼              │
│  ┌───────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Workspace │  │  AgentAdapter│  │  Notifier   │     │
│  │  Manager  │  │  (Worker)   │  │             │     │
│  └─────┬─────┘  └──────┬──────┘  └─────────────┘     │
│        │                │                              │
│        ▼                ▼                              │
│  workspace_root/   [OpenCode / Claude Code / ...]       │
│  TASK-XXX/                                           │
│    ├── prompt.txt                                    │
│    ├── result.json                                   │
│    └── _output.md                                    │
└─────────────────────────────────────────────────────────┘
                       ▲
                       │ approve / revise
                       │
              ┌────────┴────────┐
              │  Human / CLI    │
              │  (GRaffe)       │
              └─────────────────┘
```

### 3.2 Components

#### SymphonyOrchestrator (Supervisor)

The主编排器. Owns the tick loop, state machine, dispatch logic, and recovery procedures.

**Responsibilities:**
- Tick loop: polls `tasks.json`, dispatches tasks, reconciles state every N seconds (configurable)
- Task dispatch: claims a task, prepares a workspace, starts a worker, tracks the worker lifecycle
- Concurrency control: respects `max_concurrent_agents` (default: 3)
- Heartbeat monitoring: detects stalled workers via heartbeat files (mtime or sequence number)
- Retry scheduling: exponential backoff with jitter, persisted to `tasks.json` for crash recovery
- State transitions: `Todo → In Progress → Pending Review → Done/Canceled`

**Key invariant:** The Supervisor never modifies source files. It only manages workspaces and task state.

#### TasksDB (State Persistence)

Reads and writes `tasks.json`. All writes use atomic rename + file locking to prevent corruption.

**Schema:**
```json
{
  "schema_version": "1.0",
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Fix login bug",
      "description": "...",
      "state": "Todo",
      "priority": 1,
      "topic": "auth",
      "created_at": "2026-05-01T00:00:00Z",
      "updated_at": "2026-05-01T00:00:00Z",
      "acquired_at": null,
      "acquired_by": null,
      "lock_pid": null,
      "attempt_count": 0,
      "error": null,
      "result": null,
      "metadata": {},
      "retry_after": null,
      "is_retrying": false,
      "blacklisted": false
    }
  ]
}
```

**Key fields:**
- `state`: Controls dispatch eligibility. Active states: `Todo`, `In Progress`. Terminal states: `Done`, `Canceled`.
- `metadata`: Arbitrary key-value bag for task-specific configuration (e.g., `external_files`, `output_file`, `verify_after_approve`).
- `retry_after` / `is_retrying`: Exponential backoff state, persisted for restart recovery.
- `blacklisted`: Prevents a permanently failed task from being re-dispatched.

#### WorkspaceManager

Creates and manages per-task workspace directories. Enforces three invariants:

1. **Path containment:** Workspace path must be under the configured `workspace.root`.
2. **Name safety:** Directory names contain only `[A-Za-z0-9._-]`.
3. **No symlinks:** After creation, scans for symlinks and rejects the workspace if any are found.

**External File Sandbox:** When `external_files` is set in task metadata, the WorkspaceManager copies the specified files into the workspace *before* the worker starts. The worker operates on copies; changes are reviewed before being written back to the original paths.

**Lifecycle hooks:** `after_create`, `before_run`, `after_run`, `before_remove`. Each hook executes a configurable shell command with `task_id` as an argument.

#### AgentAdapter (Worker Interface)

Abstract interface that all agent runtimes must implement. Located in `EXAMPLES/opencode_centric/agent_adapter.py` as a reference implementation.

```python
class AgentAdapter(Protocol):
    def start_session(self, workspace: Workspace, prompt: str) -> Session: ...
    def wait_session(self, session: Session) -> AgentResult: ...
    def stop_session(self, session: Session) -> None: ...
    def is_process_alive(self, pid: int) -> bool: ...
```

**Session:** A handle returned by `start_session` that the Supervisor uses to track the running worker. Contains at minimum: `id`, `pid`, `workspace`, `process`.

**AgentResult:** The outcome of `wait_session`. Contains: `status` (`"success"` or `"failed"`), `exit_code`, `stdout`, `stderr`, `result` (parsed result.json).

The `OpenCodeAgent` implementation uses a Python PTY wrapper to capture TUI output and automatically respond to permission prompts, with a heartbeat daemon writing a monotonically increasing sequence number to a `.heartbeat` file for stall detection.

#### MemoryManager

Builds a sliding-window context injected into the worker's prompt, based on task `topic` and `keywords`. Maintains a `.index.md` per topic directory. On task completion, writes a memory record with timestamp; on dispatch, reads recent records to provide cross-task memory.

#### Notifier

Sends notifications when tasks enter `Pending Review`. Implementations can write to an inbox directory, POST to a webhook, or integrate with messaging platforms. Callbacks: `on_approved` and `on_revision` allow the Supervisor to react to human decisions.

---

## 4. Task State Machine

```
                    ┌──────────────────────────────────────────┐
                    │                                          │
                    ▼                                          │
┌──────┐     ┌───────────┐    worker starts    ┌────────────┐ │
│ Todo │────▶│ Claimed   │───────────────────▶│ In Progress│ │
└──────┘     └───────────┘                     └─────┬──────┘ │
                    │                                  │       │
                    │ dispatch fails                   │ done  │
                    │ or retry                         │       │
                    ▼                                  ▼       │
              ┌───────────┐                     ┌────────────┐  │
              │ Retry Q   │                     │Pending Rev. │  │
              │(exponential│                    └─────┬──────┘  │
              │ backoff) │                          │         │
              └───────────┘                          │         │
                    │                     ┌─────────┴─────────┤
                    │ retry due           │                   │
                    └─────────────────────┘                   │
                                              │  approved   ┌────┴───┐
                                              │────────────▶│  Done  │
                                              │             └───────┘
                                              │
                                              │  revised   ┌────────┐
                                              │────────────▶│  Todo  │
                                              │             └────────┘
                                              │
                                              │ max retries ┌──────────┐
                                              │────────────▶│ Canceled │
                                                           └──────────┘
```

**State descriptions:**

| State | Description |
|-------|-------------|
| `Todo` | Available for dispatch |
| `In Progress` | Worker is running |
| `Pending Review` | Worker finished, awaiting human approval |
| `Done` | Approved and cleaned up |
| `Canceled` | Permanently failed or abandoned |

**Terminal recovery:** On startup, the Supervisor scans all `In Progress` tasks. For each:
- If the `lock_pid` process is still alive → recover into `running{}` and continue monitoring.
- If the process is dead → reset to `Todo`, increment `attempt_count`.

---

## 5. Configuration Reference

All paths are **config-driven**, not hardcoded. See `EXAMPLES/opencode_centric/configs/config.yaml` for a full example.

```yaml
tracker:
  kind: "jsonfile"                    # only jsonfile for now
  tasks_file: "./symphony_data/tasks.json"

polling:
  interval_ms: 30000                  # tick interval

workspace:
  root: "./symphony_data/workspace_root"
  cleanup:
    enabled: true
    max_age_hours: 24
    check_interval_ticks: 40

memory:
  constitution_file: "./symphony_data/constitution.md"
  index_root: "./symphony_data/memory_index"
  index_max_entries: 50
  index_min_entries: 10

hooks:
  after_create: ""                   # shell command
  before_run: ""                     # shell command
  after_run: ""                      # shell command
  before_remove: ""                  # shell command
  timeout_ms: 60000

agent:
  executor: "opencode"               # adapter name
  opencode:
    command: "opencode --agent"
    max_turns: 20
  max_concurrent_agents: 3
  max_retry_backoff_ms: 300000       # 5 minutes

timeouts:
  read_timeout_ms: 5000
  turn_timeout_ms: 3600000           # 1 hour per turn
  stall_timeout_ms: 300000           # 5 minutes without heartbeat

logging:
  level: "INFO"
  file: "./symphony_data/logs/symphony.log"
  max_size_mb: 10

sandbox:
  enabled: true
  backup_orig: true
  audit_log: true

verify:
  auto_enabled: false
  threshold: 80
```

---

## 6. Task Metadata Reference

Task metadata is a free-form key-value bag. Some keys have special meaning:

| Key | Type | Description |
|-----|------|-------------|
| `external_files` | `list[dict]` | Files to copy into workspace before run. Each entry: `{original: str, workspace_copy: str}` |
| `output_file` | `str` | File to copy to inbox directory on success |
| `verify_after_approve` | `bool` | Run auto-verification after approval |
| `topic` | `str` | Memory topic for context injection |
| `keywords` | `list[str]` | Keywords for memory filtering |
| `repo_url` | `str` | Available for `after_create` hook scripts |

---

## 7. Replacing the Agent Runtime

OpenCode is a reference implementation, not the only option. To swap in a different agent:

### 7.1 Implement the AgentAdapter Interface

The shared types are defined in `FRAMEWORK/agent_types.py`. Your adapter should implement the interface defined there:

```python
from .agent_types import AgentAdapter, Session, AgentResult
```

To see a complete implementation, refer to `EXAMPLES/opencode_centric/agent_adapter.py`.

### 7.2 Update config.yaml

```yaml
agent:
  executor: "your_agent"
  your_agent:
    command: "your-agent --mode cli"
    max_turns: 20
```

### 7.3 Register the Adapter in symphony_core.py

In the `start()` method, add your adapter import and initialization:

```python
from your_agent_adapter import YourAgent
self.agent_adapter = YourAgent(self.config)
```

### 7.4 Known Compatible Agents

| Agent | Adapter Complexity | Notes |
|-------|-------------------|-------|
| **OpenCode** | Provided | Reference implementation with PTY wrapper and permission auto-response |
| **Claude Code** | Medium | Requires `--print` or structured output mode; no native permission API |
| **OpenAI Codex** | Medium | CLI mode similar to OpenCode |
| **Custom script** | Low | Any script that reads `prompt.txt` and writes `result.json` |

---

## 8. External File Sandbox Deep Dive

The External File Sandbox allows agents to modify production files without operating on them directly.

### Flow

```
before_run hook                    agent runs                    approval
    │                                  │                            │
    ▼                                  ▼                            ▼
copy external files          agent edits copies in           write copies back
into workspace               workspace (unaware of           to original paths
                             originals)
```

### Example Task

```json
{
  "id": "TASK-FIX-AUTH",
  "title": "Fix authentication bug",
  "description": "Modify the login function in auth.py to handle expired tokens.",
  "state": "Todo",
  "metadata": {
    "external_files": [
      {
        "original": "/path/to/production/auth.py",
        "workspace_copy": "auth.py"
      }
    ],
    "verify_after_approve": true
  }
}
```

The agent sees only `auth.py` in its workspace. After approval, the Supervisor copies the modified `auth.py` back to `/path/to/production/auth.py`.

---

## 9. Startup Reconciliation Algorithm

On every startup, the Supervisor rebuilds its in-memory state from `tasks.json`:

```
1. Scan all tasks with state = "In Progress"
2. For each task:
   a. If lock_pid is null → reset to Todo (crashed before acquiring lock)
   b. If lock_pid process is alive → recover into running{} with attempt_count
   c. If lock_pid process is dead → reset to Todo, increment attempt_count
3. Scan all tasks with is_retrying = true:
   a. If retry_after has passed → clear is_retrying, add to dispatch pool
   b. If retry_after is future → rebuild in-memory retry entry
```

This makes crashes transparent. No task silently disappears; every failure is surfaced and recoverable.

---

## 10. Key Invariants and Safety Properties

1. **Single-instance enforcement:** A file lock (`.instance.lock`) prevents multiple Supervisor processes from running simultaneously.
2. **Atomic state writes:** All `tasks.json` writes use rename-from-temp + exclusive file lock.
3. **Attempt count monotonicity:** `attempt_count` never decreases, even on reset.
4. **Blacklist persistence:** A blacklisted task is written to `tasks.json` and survives Supervisor restarts.
5. **Grace period:** Newly dispatched tasks have a grace period (default: 60s) before heartbeat stall detection kicks in.
6. **No symlink workspaces:** Workspace creation fails if symlinks are detected, preventing path traversal attacks.
