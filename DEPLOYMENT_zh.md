# Symphony-Lite 部署指南

> 部署 Symphony-Lite 的分步实践指南。各阶段按依赖关系排序：完成当前阶段后再进入下一阶段。

---

## 阶段概览

| 阶段 | 重点 | 预计时间 |
|------|------|----------|
| **Phase 1** | 骨架：最小化编排器 + 任务持久化 | 30–60 分钟 |
| **Phase 2** | Workspace 隔离 + 并发控制 | 20–30 分钟 |
| **Phase 3** | 完整状态机 + 重试 + 崩溃恢复 | 30–45 分钟 |
| **Phase 4** | 记忆系统 + 通知机制 | 20–30 分钟 |
| **Phase 5** | 外部文件沙盒 + 自动验收 | 30–45 分钟 |
| **Phase 6** | 替换 OpenCode 为其他 Agent | 因人而异 |

---

## 开始之前

### 环境要求

- Python 3.10+
- 支持 JSON 文件读写的任务追踪器
- 至少一个支持 CLI 的 AI 编程 Agent（首次部署推荐 OpenCode）

### 目录结构

```
symphony_lite/
├── FRAMEWORK/                      # 框架核心代码（与具体 Agent 无关）
│   ├── symphony_core.py              # 编排器
│   ├── tasks_db.py                  # 任务持久化
│   ├── workspace_mgr.py              # Workspace 管理
│   ├── config_loader.py              # 配置加载
│   ├── memory_manager.py             # 记忆系统
│   ├── notifier.py                  # 通知系统
│   ├── file_watcher.py               # 任务文件监听
│   ├── logger.py                    # 日志
│   ├── utils.py                     # 工具函数
│   └── __init__.py
├── EXAMPLES/
│   └── opencode_centric/             # OpenCode 参考实现
│       ├── agent_adapter.py          # OpenCode 适配器
│       ├── opencode_pty_wrapper.py   # PTY 封装脚本
│       ├── configs/
│       │   └── config.yaml           # 示例配置
│       └── scripts/
│           └── run_orchestrator.py   # 启动脚本
└── DOCS/
```

---

## Phase 1 — 骨架：最小化编排器 + 任务持久化

**目标：** 让 tick 循环跑起来，处理单个任务。Supervisor 启动后读取 `tasks.json`，无任务时静默等待。

**为什么最先做这个：** Tick 循环和任务 DB 是一切的基础。这些跑通了，才能逐步添加功能而不破坏根基。

### Step 1.1 — 创建数据目录

```bash
mkdir -p symphony_data
```

### Step 1.2 — 创建空的 tasks.json

```bash
touch symphony_data/tasks.json
```

填入内容：

```json
{
  "schema_version": "1.0",
  "tasks": []
}
```

### Step 1.3 — 创建 config.yaml

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

### Step 1.4 — 创建 Stub Agent 适配器

在 `EXAMPLES/opencode_centric/` 下创建 `agent_stub.py`：

```python
"""Phase 1 测试用 Stub Agent — 替代真实 Agent 执行简单操作。"""
import os
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

### Step 1.5 — 将 Stub 接入 symphony_core.py

在 `FRAMEWORK/symphony_core.py` 的 `start()` 方法中，替换 OpenCode 适配器为 Stub：

```python
# from .agent_adapter import OpenCodeAgent  # 注释掉
from EXAMPLES.opencode_centric.agent_stub import StubAgent as OpenCodeAgent

self.agent_adapter = OpenCodeAgent(self.config)
```

### Step 1.6 — 创建启动脚本

在 `EXAMPLES/opencode_centric/scripts/` 下创建 `run_orchestrator.py`：

```python
#!/usr/bin/env python3
"""Symphony-Lite 编排器最小化启动脚本。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "FRAMEWORK"))

from symphony_core import SymphonyOrchestrator
from config_loader import load_config

config = load_config("path/to/your/config.yaml")
orchestrator = SymphonyOrchestrator(config)
orchestrator.start()
```

### Step 1.7 — 运行并验证

```bash
python3 EXAMPLES/opencode_centric/scripts/run_orchestrator.py
```

**预期行为：**
- 启动无报错
- 日志输出 "Symphony-Lite starting..."
- 无任务可分发，tick 循环静默运行
- Ctrl+C 正常退出

**验证命令：**
```bash
# 检查日志文件存在
ls -la symphony_data/logs/symphony.log

# 检查 tasks.json 仍是合法 JSON
python3 -c "import json; json.load(open('symphony_data/tasks.json'))"
```

### 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: No module named 'symphony_core'` | 路径问题 | 从项目根目录运行，或设置 `PYTHONPATH=.` |
| `FileNotFoundError: ./symphony_data/tasks.json` | 工作目录错误 | 配置中使用绝对路径，或从项目根目录运行 |
| Tick 循环立即退出 | `_tick()` 中有异常 | 查看日志文件中的堆栈跟踪 |

---

## Phase 2 — Workspace 隔离 + 并发控制

**目标：** 每个分发的任务拥有独立的 workspace 目录。在并发限制内支持多任务并行。

**为什么在 Phase 3 之前做：** Workspace 隔离防止任务间干扰，是测试完整状态机的前提。

### Step 2.1 — 创建数据子目录

```bash
mkdir -p symphony_data/workspace_root symphony_data/logs
touch symphony_data/constitution.md
```

### Step 2.2 — 向 tasks.json 添加一个真实任务

```json
{
  "schema_version": "1.0",
  "tasks": [
    {
      "id": "TEST-001",
      "title": "Phase 2 测试任务",
      "description": "将当前日期写入 workspace 中的一个文件。",
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

### Step 2.3 — 升级 Stub Agent 真正执行工作

将 Stub 替换为真正写文件的脚本：

```python
import subprocess, time, os, shutil

class StubAgent:
    def start_session(self, workspace, prompt):
        pid = os.fork()
        if pid == 0:
            # child: 执行工作
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

### Step 2.4 — 运行并验证

```bash
python3 EXAMPLES/opencode_centric/scripts/run_orchestrator.py
```

**预期行为：**
- 编排器从 tasks.json 认领 TEST-001
- 创建 `symphony_data/workspace_root/TEST-001/` 目录
- 在其中写入 `_output.md`
- TEST-001 状态变为 `Pending Review`
- 日志显示 `Dispatched TEST-001`

**验证命令：**
```bash
# 检查 workspace 被创建且有输出
ls -la symphony_data/workspace_root/TEST-001/
cat symphony_data/workspace_root/TEST-001/_output.md

# 检查任务状态变化
python3 -c "import json; t=json.load(open('symphony_data/tasks.json')); print([x['state'] for x in t['tasks']])"
```

---

## Phase 3 — 完整状态机 + 重试 + 崩溃恢复

**目标：** 实现完整状态机：Todo → In Progress → Pending Review → Done/Canceled，包含失败重试和重启恢复。

**为什么这个阶段靠前：** 这是框架的核心价值。没有可靠的状态转换和重试，系统无法在生产环境信任。

### Step 3.1 — 实现完整的 _dispatch 流程

确保 `_dispatch()` 处理：
- 认领任务（将 `acquired_at`、`acquired_by`、`lock_pid` 写入 `tasks.json`）
- 准备 workspace
- 启动 agent session
- 在 `running{}` 中跟踪 session

### Step 3.2 — 实现 _on_agent_done

确保 `AgentResult` 正确映射：
- `status=success` + workspace 有输出文件 → `Pending Review`
- `status=failed` 或无输出 → 重试或 `Canceled`（超过最大次数时）

### Step 3.3 — 实现心跳监控

`_reconcile_running()` 方法需要：
- 每 tick 检查心跳文件 mtime
- kill 超过 `stall_timeout_ms` 未响应的 worker
- 将被 kill 的任务标记为重试

### Step 3.4 — 实现指数退避重试

`_schedule_retry()` 需要：
- 计算退避时间：`delay_ms = min(10_000 * 2 ** (attempt - 1), max_retry_backoff_ms)`
- 将 `retry_after` 和 `is_retrying` 写入 `tasks.json`（保证重启后可恢复）

### Step 3.5 — 实现启动对账

`_startup_reconciliation()` 每次启动时：
- 扫描所有 `In Progress` 任务
- 检查 `lock_pid` 是否存活
- 恢复运行中的任务或重置卡死的任务

### Step 3.6 — 测试重试行为

1. 创建一个总是失败的任务
2. 验证它以退避重试直到 10 次
3. 10 次后确认进入 `Canceled`

### Step 3.7 — 测试崩溃恢复

1. 启动编排器，让它分发一个任务
2. 用 `kill -9` 杀死编排器进程
3. 重启编排器
4. 验证任务被恢复或正确重置

---

## Phase 4 — 记忆系统 + 通知机制

**目标：** 将过往任务的相关上下文注入到新任务的 prompt 中。在任务进入 `Pending Review` 时发送通知。

**为什么在 Phase 5 之前做：** 记忆减少重复劳动；通知使人工程序化审批切实可行。

### Step 4.1 — 记忆上下文注入

`MemoryManager.build_filtered_context(topic, keywords)` 方法需要：
- 从 `memory_index/{topic}/.index.md` 读取近期条目
- 返回一个追加到任务 prompt 的 markdown 字符串

### Step 4.2 — Pending Review 通知

`Notifier.notify_task_pending_review(task)` 方法需要：
- 写入 inbox 目录的文件，或
- POST 到 webhook URL，或
- 集成你的消息平台

### Step 4.3 — 审批和打回调职

将 `on_approved` 和 `on_revision` 回调接入 Notifier，使人工决策触发状态转换。

---

## Phase 5 — 外部文件沙盒 + 自动验收

**目标：** 允许 Agent 安全地修改生产文件，在写回前进行自动验证。

**为什么最后做：** 它要求前所有阶段都正确工作。先把基础打牢。

### Step 5.1 — 在 task metadata 中配置外部文件

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

### Step 5.2 — 实现 before_run 文件注入

在 `WorkspaceManager` 中添加 `_prepare_workspace()`：

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

在 `symphony_core._dispatch()` 中，Agent 启动前调用此方法。

### Step 5.3 — 实现 after_approve 写回

在 `SymphonyOrchestrator._on_graffe_approved()` 中：

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

### Step 5.4 — 接入自动验收（可选）

如果 task metadata 中设置 `verify_after_approve: true`：
1. Agent 完成后，运行验收评分器（如 momment）
2. 分数 ≥ 阈值 → 自动审批通过
3. 分数 < 阈值 → 自动打回重做

---

## Phase 6 — 替换 OpenCode 为其他 Agent

OpenCode 适配器位于 `EXAMPLES/opencode_centric/agent_adapter.py`。替换它只需三步：

### Step 6.1 — 实现 AgentAdapter 接口

创建新文件，如 `EXAMPLES/claude_code_centric/agent_adapter.py`：

```python
"""Claude Code 适配器 for Symphony-Lite."""
import subprocess, json, os, signal
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
        result = {}
        result_file = os.path.join(session.workspace.path, "result.json")
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
            os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)

    def is_process_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
```

### Step 6.2 — 更新 config.yaml

```yaml
agent:
  executor: "claude_code"
  claude_code:
    command: "claude --print"
    max_turns: 20
```

### Step 6.3 — 更新 symphony_core.py

在 `start()` 中，修改导入和初始化：

```python
# from .agent_adapter import OpenCodeAgent
from EXAMPLES.claude_code_centric.agent_adapter import ClaudeCodeAgent as OpenCodeAgent

self.agent_adapter = OpenCodeAgent(self.config)
```

### Step 6.4 — 验证

运行现有任务，确认行为与 OpenCode 基线一致。

---

## 配置检查清单

上线生产环境前，确认以下配置：

- [ ] `tracker.tasks_file` — 指向**有备份**的目录
- [ ] `workspace.root` — 有足够磁盘空间容纳并发任务
- [ ] `logging.level` — 生产用 `INFO`（`DEBUG` 会产生大量日志）
- [ ] `logging.max_size_mb` — 设置合理大小并配合日志轮转
- [ ] `agent.max_concurrent_agents` — 初始值低（1–3），确认稳定后再增加
- [ ] `timeouts.stall_timeout_ms` — 高于你预期的任务执行时间
- [ ] `sandbox.enabled` — 若 Agent 修改生产文件，必须为 `true`
- [ ] `verify.auto_enabled` — 仅在充分测试后开启
- [ ] `instance.lock_path` — 设置在本地文件系统上（不要用 NFS）
