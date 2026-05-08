# Symphony-Lite Deployment Guide

> Practical step-by-step guide to deploying Symphony-Lite. Phases are ordered by dependency: complete each phase before moving to the next.

---

## Phase Overview

| Phase | Focus | Time Estimate |
|-------|-------|--------------|
| **Phase 1** | Skeleton: minimal orchestrator + task persistence | 30–60 min |
| **Phase 2** | Workspace isolation + concurrency | 20–30 min |
| **Phase 3** | Full state machine + retry + recovery | 30–45 min |
| **Phase 4** | Memory system + notification | 20–30 min |
| **Phase 5** | External File Sandbox + auto-verify | 30–45 min |
| **Phase 6** | Replace OpenCode with a different agent | Varies |

---

## Before You Begin

### Prerequisites

- Python 3.10+
- A task tracker compatible with JSON file I/O
- At least one CLI-capable AI coding agent (OpenCode recommended for first deployment)

### Required Files

All framework core files are in `FRAMEWORK/`. All OpenCode-specific reference files are in `EXAMPLES/opencode_centric/`.

```
symphony_lite/
├── FRAMEWORK/
│   ├── symphony_core.py          # Orchestrator
│   ├── tasks_db.py              # Task persistence
│   ├── workspace_mgr.py          # Workspace management
│   ├── config_loader.py          # Config loading
│   ├── memory_manager.py          # Memory system
│   ├── notifier.py              # Notification system
│   ├── file_watcher.py           # Task file watching
│   ├── logger.py                # Logging
│   ├── utils.py                 # Utilities
│   └── __init__.py
├── EXAMPLES/
│   └── opencode_centric/
│       ├── agent_adapter.py     # OpenCode adapter
│       ├── opencode_pty_wrapper.py
│       ├── configs/
│       │   └── config.yaml
│       └── scripts/
│           └── run_orchestrator.py
└── DOCS/
```

---

## Phase 1 — Skeleton: Minimal Orchestrator + Task Persistence

**Goal:** Get the tick loop running with a single task. The Supervisor should start, read `tasks.json`, and do nothing if no tasks are available.

**Why this first:** The tick loop and task DB are the backbone of everything else. If these work, you can add features incrementally without breaking the foundation.

### Step 1.1 — Create the data directory

```bash
mkdir -p symphony_data
```

### Step 1.2 — Create initial tasks.json

```bash
touch symphony_data/tasks.json
```

Write this content:

```json
{
  "schema_version": "1.0",
  "tasks": []
}
```

### Step 1.3 — Create config.yaml

```yaml
tracker:
  kind: "jsonfile"
  tasks_file: "./symphony_data/tasks.json"

polling:
  interval_ms: 30000

workspace:
  root: "./symphony_data/workspace_root"
  cleanup:
    enabled: false

memory:
  constitution_file: "./symphony_data/constitution.md"
  index_root: "./symphony_data/memory_index"
  index_max_entries: 50
  index_min_entries: 10

agent:
  executor: "opencode"
  opencode:
    command: "echo 'stub'"
    max_turns: 1
  max_concurrent_agents: 1
  max_retry_backoff_ms: 300000

timeouts:
  read_timeout_ms: 5000
  turn_timeout_ms: 30000
  stall_timeout_ms: 60000

logging:
  level: "INFO"
  file: "./symphony_data/logs/symphony.log"
  max_size_mb: 10

sandbox:
  enabled: false

verify:
  auto_enabled: false
  threshold: 80
```

### Step 1.4 — Create stub agent adapter

In `EXAMPLES/opencode_centric/`, create `agent_stub.py`:

```python
"""Stub agent for Phase 1 testing — replaces real agent with a simple echo."""
from dataclasses import dataclass

@dataclass
class Session:
    id: str
    pid: int
    workspace: "Workspace | None" = None
    process: None = None

@dataclass
class AgentResult:
    status: str
    exit_code: int
    stdout: str
    stderr: str
    result: dict

class StubAgent:
    def start_session(self, workspace, prompt):
        return Session(id="stub-1", pid=99999, workspace=workspace)

    def wait_session(self, session):
        return AgentResult(
            status="success",
            exit_code=0,
            stdout="stub output",
            stderr="",
            result={},
        )

    def stop_session(self, session):
        pass

    def is_process_alive(self, pid):
        return True
```

### Step 1.5 — Wire stub adapter into symphony_core.py

In `FRAMEWORK/symphony_core.py`, find the `start()` method. Replace the OpenCode adapter with your stub:

```python
# from .agent_adapter import OpenCodeAgent  # COMMENT OUT
from EXAMPLES.opencode_centric.agent_stub import StubAgent as OpenCodeAgent

self.agent_adapter = OpenCodeAgent(self.config)
```

### Step 1.6 — Create the run script

In `EXAMPLES/opencode_centric/scripts/`, create `run_orchestrator.py`:

```python
#!/usr/bin/env python3
"""Minimal launcher for Symphony-Lite orchestrator."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "FRAMEWORK"))

from symphony_core import SymphonyOrchestrator
from config_loader import load_config

config = load_config("path/to/your/config.yaml")
orchestrator = SymphonyOrchestrator(config)
orchestrator.start()
```

### Step 1.7 — Run and verify

```bash
python3 EXAMPLES/opencode_centric/scripts/run_orchestrator.py
```

**Expected behavior:**
- Starts without errors
- Logs "Symphony-Lite starting..."
- No tasks to dispatch, tick loop keeps running silently
- Press Ctrl+C to stop

**Verification commands:**
```bash
# Check log file exists
ls -la symphony_data/logs/symphony.log

# Check tasks.json is still valid JSON
python3 -c "import json; json.load(open('symphony_data/tasks.json'))"
```

### Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'symphony_core'` | Path issue | Run from project root, or set `PYTHONPATH=.` |
| `FileNotFoundError: ./symphony_data/tasks.json` | Wrong working directory | Use absolute paths in config, or launch from project root |
| Tick loop exits immediately | Exception in `_tick()` | Check log file for stack trace |

---

## Phase 2 — Workspace Isolation + Concurrency

**Goal:** Each dispatched task gets its own workspace directory. Multiple tasks can run concurrently within the concurrency limit.

**Why this before Phase 3:** Workspace isolation prevents task interference and is required before you can safely test the full state machine.

### Step 2.1 — Create data subdirectories

```bash
mkdir -p symphony_data/workspace_root symphony_data/logs
touch symphony_data/constitution.md
```

### Step 2.2 — Add a real task to tasks.json

```json
{
  "schema_version": "1.0",
  "tasks": [
    {
      "id": "TEST-001",
      "title": "Phase 2 test task",
      "description": "Write the current date to a file in the workspace.",
      "state": "Todo",
      "priority": 1,
      "topic": "test",
      "created_at": "2026-05-08T00:00:00Z",
      "updated_at": "2026-05-08T00:00:00Z",
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

### Step 2.3 — Upgrade stub agent to do real work

Replace the stub with a script that actually writes a file:

```python
import subprocess, time, os

class StubAgent:
    def start_session(self, workspace, prompt):
        pid = os.fork()
        if pid == 0:
            # child: do work
            time.sleep(1)
            with open(os.path.join(workspace.path, "_output.md"), "w") as f:
                f.write(f"# Task Output\n\nDate: {time.strftime('%Y-%m-%d')}\n")
            exit(0)
        return Session(id=f"{pid}", pid=pid, workspace=workspace)

    def wait_session(self, session):
        _, exit_code = os.waitpid(session.pid, 0)
        return AgentResult(
            status="success" if exit_code == 0 else "failed",
            exit_code=exit_code,
            stdout="done",
            stderr="",
            result={},
        )
```

### Step 2.4 — Run and verify

```bash
python3 EXAMPLES/opencode_centric/scripts/run_orchestrator.py
```

**Expected behavior:**
- Orchestrator picks up TEST-001 from tasks.json
- Creates `symphony_data/workspace_root/TEST-001/` directory
- Writes `_output.md` inside it
- TEST-001 state → `Pending Review`
- Log shows `Dispatched TEST-001`

**Verification commands:**
```bash
# Check workspace was created and has output
ls -la symphony_data/workspace_root/TEST-001/
cat symphony_data/workspace_root/TEST-001/_output.md

# Check task state changed
python3 -c "import json; t=json.load(open('symphony_data/tasks.json')); print([x['state'] for x in t['tasks']])"
```

---

## Phase 3 — Full State Machine + Retry + Recovery

**Goal:** Implement the complete state machine: Todo → In Progress → Pending Review → Done/Canceled, with retry on failure and crash recovery on restart.

**Why this next:** This is the core value proposition. Without reliable state transitions and retry, you cannot trust the system in production.

### Step 3.1 — Implement the complete _dispatch flow

Ensure `_dispatch()` in `symphony_core.py` handles:
- Claiming a task (write `acquired_at`, `acquired_by`, `lock_pid` to `tasks.json`)
- Preparing the workspace
- Starting the agent session
- Tracking the session in `running{}`

### Step 3.2 — Implement _on_agent_done

Ensure `AgentResult` outcomes map correctly:
- `status=success` + workspace has output files → `Pending Review`
- `status=failed` or no output → retry or `Canceled` (if max attempts exceeded)

### Step 3.3 — Implement heartbeat monitoring

The `_reconcile_running()` method should:
- Check heartbeat file mtime every tick
- Kill workers that have been stalled for longer than `stall_timeout_ms`
- Mark killed tasks for retry

### Step 3.4 — Implement retry with exponential backoff

`_schedule_retry()` should:
- Calculate backoff: `delay_ms = min(10_000 * 2 ** (attempt - 1), max_retry_backoff_ms)`
- Write `retry_after` and `is_retrying` to `tasks.json` so restart recovery works

### Step 3.5 — Implement startup reconciliation

`_startup_reconciliation()` should on every start:
- Scan all `In Progress` tasks
- Check if `lock_pid` is alive
- Recover running tasks or reset stalled ones

### Step 3.6 — Test retry behavior

1. Create a task that always fails (bad prompt or crash):
```json
{
  "id": "FAIL-001",
  "title": "Always fails",
  "description": "exit 1",
  "state": "Todo",
  ...
}
```
2. Verify it retries up to 10 times with backoff
3. After 10 retries, verify it goes to `Canceled`

### Step 3.7 — Test crash recovery

1. Start orchestrator, let it dispatch a task
2. Kill the orchestrator process with `kill -9`
3. Restart the orchestrator
4. Verify the task is recovered or properly reset

---

## Phase 4 — Memory System + Notification

**Goal:** Inject relevant context from past tasks into new task prompts. Notify when tasks enter `Pending Review`.

**Why this before Phase 5:** Memory reduces redundant work; notifications make human-in-the-loop practical.

### Step 4.1 — Memory context injection

The `MemoryManager.build_filtered_context(topic, keywords)` method should:
- Read recent entries from `memory_index/{topic}/.index.md`
- Return a markdown string that gets appended to the task prompt

### Step 4.2 — Notification on Pending Review

The `Notifier.notify_task_pending_review(task)` method should:
- Write a file to the inbox directory, or
- POST to a webhook URL, or
- Integrate with your messaging platform

### Step 4.3 — Approval and revision callbacks

Wire `on_approved` and `on_revision` callbacks into the Notifier so human decisions trigger state transitions.

---

## Phase 5 — External File Sandbox + Auto-Verify

**Goal:** Allow agents to modify production files safely, with automatic verification before write-back.

**Why this last:** It requires all previous phases to be working correctly. Build the foundation first.

### Step 5.1 — Configure external files in task metadata

```json
{
  "id": "PROD-FIX-001",
  "metadata": {
    "external_files": [
      {
        "original": "/path/to/production/config.yaml",
        "workspace_copy": "config.yaml"
      }
    ]
  }
}
```

### Step 5.2 — Implement before_run file injection

In `WorkspaceManager`, add `_prepare_workspace()`:
```python
def _prepare_workspace(self, workspace_path, task_id, metadata):
    external_files = metadata.get("external_files", [])
    for item in external_files:
        original = item["original"]
        copy_name = item["workspace_copy"]
        dest = os.path.join(workspace_path, copy_name)
        if os.path.exists(original):
            shutil.copy2(original, dest)
```

Call this in `symphony_core._dispatch()` before starting the agent.

### Step 5.3 — Implement after_approve write-back

In `SymphonyOrchestrator._on_graffe_approved()`:
```python
def _on_graffe_approved(self, task):
    workspace_path = self.config.get_workspace_path(task.id)
    self.workspace_mgr.deploy_sandboxed_files(
        workspace_path=workspace_path,
        task_id=task.id,
        metadata=task.metadata,
        config=self.config,
    )
```

### Step 5.4 — Integrate auto-verify (optional)

If `verify_after_approve: true` in task metadata:
1. After agent completes, run your verification scorer (e.g., momment)
2. If score ≥ threshold → auto-approve
3. If score < threshold → auto-revise

---

## Phase 6 — Replacing OpenCode with Another Agent

The OpenCode adapter is in `EXAMPLES/opencode_centric/agent_adapter.py`. To replace it:

### Step 6.1 — Implement the AgentAdapter interface

Create a new file, e.g., `EXAMPLES/claude_code_centric/agent_adapter.py`:

```python
"""Claude Code adapter for Symphony-Lite."""
import subprocess, json, os, time
from dataclasses import dataclass

@dataclass
class Session:
    id: str
    pid: int
    workspace: "Workspace"
    process: "subprocess.Popen | None" = None

@dataclass
class AgentResult:
    status: str
    exit_code: int
    stdout: str
    stderr: str
    result: dict

class ClaudeCodeAgent:
    def __init__(self, config):
        self.config = config
        self.command = config.agent.claude_code.command
        self.max_turns = config.agent.claude_code.max_turns

    def start_session(self, workspace, prompt):
        prompt_file = os.path.join(workspace.path, "prompt.txt")
        with open(prompt_file, "w") as f:
            f.write(prompt)

        proc = subprocess.Popen(
            ["bash", "-c", f"{self.command} < {prompt_file}"],
            cwd=workspace.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        return Session(id=f"{proc.pid}", pid=proc.pid, workspace=workspace, process=proc)

    def wait_session(self, session):
        stdout, stderr = session.process.communicate()
        result_file = os.path.join(session.workspace.path, "result.json")
        result = {}
        if os.path.exists(result_file):
            result = json.loads(open(result_file).read())
        return AgentResult(
            status="success" if session.process.returncode == 0 else "failed",
            exit_code=session.process.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
            result=result,
        )

    def stop_session(self, session):
        if session.process:
            import signal
            os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)

    def is_process_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
```

### Step 6.2 — Update config

```yaml
agent:
  executor: "claude_code"
  claude_code:
    command: "claude --print"
    max_turns: 20
```

### Step 6.3 — Update symphony_core.py

In `start()`, change the import and initialization:

```python
# from .agent_adapter import OpenCodeAgent
from EXAMPLES.claude_code_centric.agent_adapter import ClaudeCodeAgent as OpenCodeAgent

self.agent_adapter = OpenCodeAgent(self.config)
```

### Step 6.4 — Verify

Run your existing task suite and confirm behavior is identical to the OpenCode baseline.

---

## Configuration Checklist

Before going to production, verify these settings:

- [ ] `tracker.tasks_file` — Points to a directory that is **backed up**
- [ ] `workspace.root` — Has sufficient disk space for concurrent tasks
- [ ] `logging.level` — `INFO` in production (not `DEBUG`, which generates huge logs)
- [ ] `logging.max_size_mb` — Set to a reasonable size with log rotation
- [ ] `agent.max_concurrent_agents` — Start low (1–3), increase as you verify stability
- [ ] `timeouts.stall_timeout_ms` — Set higher than your expected task duration
- [ ] `sandbox.enabled` — `true` if agents modify production files
- [ ] `verify.auto_enabled` — Only enable after thorough testing
- [ ] `instance.lock_path` — Set to a path on a local filesystem (not NFS)
