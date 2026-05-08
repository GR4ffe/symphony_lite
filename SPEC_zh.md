# Symphony-Lite：Agent 编排框架规范

> 一个轻量级、状态驱动的框架，用于在协调工作流中编排多个 AI 编程 Agent。支持自主任务执行同时保留人工干预节点。

---

## 1. 背景与动机

### 问题所在

单 Agent 系统在复杂多步任务上很快遇到瓶颈。一个独立的 Agent 必须同时理解上下文、规划步骤、执行工具、管理记忆和处理错误——导致上下文过载、可靠性下降、以及缺乏任务委派能力。

### 核心观察

瓶颈在于**协调**，而非能力。最强大的 AI 编程 Agent（OpenCode、Claude Code、Codex）本身已经有足够的能力。缺的是一个**结构化的运行环境**，能够：

- 将工作拆分为离散、可验证的任务
- 在 Agent 重启后保持持久状态
- 将 Agent 的变更隔离在审批流程中
- 提供跨任务边界的记忆能力
- 自动处理权限提示

### 设计思路

Symphony-Lite 将多 Agent 系统中的 **Supervisor-Worker** 模式应用于自主任务执行。轻量级编排器（"Supervisor"）管理任务生命周期，委托给可插拔的 Agent 运行时（"Workers"），并执行安全边界。系统可以无人值守运行，同时在每个关键决策点保留人工监督。

---

## 2. 设计理念

### 原则一：职责分离

Supervisor 从不自己做编码工作。它只做编排：决定下一个运行什么、向 Workers 输入上下文、监控进度、执行状态转换。Workers（OpenCode、Claude Code 等）专注于执行本身。

### 原则二：状态是单一真相源

所有任务状态存在于一个 `tasks.json` 文件中。Supervisor 的内存状态在启动时（对账阶段）从该文件推导，重启安全。没有任务会被丢弃；崩溃可恢复，不是消失。

### 原则三：沙盒 + 审批机制

Workers 永远不直接修改生产文件。它们在隔离的 workspace 目录中工作。所有变更在写回之前都要经过审查（人工或自动）。这使得系统天然适合无人值守运行。

### 原则四：可插拔 Agent 运行时

Worker 层抽象为 `AgentAdapter` 接口。OpenCode 是该接口的一个实现。其他人（Claude Code、Codex、自定义 Agent）可在不触动 Supervisor 或状态机的情况下接入。

### 原则五：显式重试，有上限

失败不静默。指数退避引导重试，硬性上限（默认：10 次）防止无限循环。每次尝试都有日志且可恢复。

---

## 3. 架构

### 3.1 系统拓扑

```
┌─────────────────────────────────────────────────────────┐
│                    tasks.json                            │
│           （单一真相源，基于文件）                          │
└──────────────────────┬──────────────────────────────────┘
                       │ 轮询 / 监听
                       ▼
┌─────────────────────────────────────────────────────────┐
│               SymphonyOrchestrator                       │
│                 （Supervisor / Tick 循环）                │
│  ┌──────────────────────────────────────────────────┐  │
│  │  内存状态：claimed{}  running{}  retries{}         │  │
│  └──────────────────────────────────────────────────┘  │
│         │              │               │              │
│         ▼              ▼               ▼              │
│  ┌───────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ Workspace │  │ AgentAdapter│  │  Notifier   │     │
│  │  Manager  │  │  (Worker)  │  │             │     │
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
              │  人工 / CLI     │
              └─────────────────┘
```

### 3.2 组件说明

#### SymphonyOrchestrator（主编排器）

主编排器。拥有 tick 循环、状态机、调度逻辑和恢复程序。

**职责：**
- Tick 循环：轮询 `tasks.json`，分发任务，每 N 秒（可配置）对账一次
- 任务分发：认领任务、准备 workspace、启动 worker、跟踪 worker 生命周期
- 并发控制：遵守 `max_concurrent_agents`（默认：3）
- 心跳监控：通过心跳文件（mtime 或序列号）检测卡死的 worker
- 重试调度：指数退避，持久化到 `tasks.json`，崩溃可恢复
- 状态转换：`Todo → In Progress → Pending Review → Done/Canceled`

**核心不变量：** Supervisor 永远不修改源文件，只管理 workspace 和任务状态。

#### TasksDB（状态持久化）

读写 `tasks.json`。所有写操作使用原子 rename + 文件锁，防止损坏。

**Schema：**
```json
{
  "schema_version": "1.0",
  "tasks": [
    {
      "id": "TASK-001",
      "title": "修复登录 Bug",
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

**关键字段：**
- `state`：控制分发资格。活跃状态：`Todo`、`In Progress`。终态：`Done`、`Canceled`。
- `metadata`：任务特定的自由键值对（如 `external_files`、`output_file`、`verify_after_approve`）。
- `retry_after` / `is_retrying`：指数退避状态，持久化以便崩溃恢复。
- `blacklisted`：防止永久失败的任务被重新分发。

#### WorkspaceManager

创建和管理每个任务的 workspace 目录。强制三个不变量：

1. **路径包含性：** Workspace 路径必须在配置的 `workspace.root` 之下。
2. **名称安全性：** 目录名只允许 `[A-Za-z0-9._-]`。
3. **禁止软链接：** 创建后扫描软链接，若存在则拒绝该 workspace。

**外部文件沙盒：** 当 task metadata 中设定了 `external_files` 时，WorkspaceManager 在 worker 启动前将指定文件复制到 workspace 中。Worker 操作的是副本；变更在审批后才写回到原始路径。

**生命周期钩子：** `after_create`、`before_run`、`after_run`、`before_remove`。每个钩子执行一个可配置的 shell 命令，`task_id` 作为参数传入。

#### AgentAdapter（Worker 接口）

所有 Agent 运行时必须实现的抽象接口。参考实现在 `EXAMPLES/opencode_centric/agent_adapter.py`。

```python
class AgentAdapter(Protocol):
    def start_session(self, workspace: Workspace, prompt: str) -> Session: ...
    def wait_session(self, session: Session) -> AgentResult: ...
    def stop_session(self, session: Session) -> None: ...
    def is_process_alive(self, pid: int) -> bool: ...
```

**Session：** `start_session` 返回的句柄，Supervisor 用它跟踪运行中的 worker。至少包含：`id`、`pid`、`workspace`、`process`。

**AgentResult：** `wait_session` 的结果。包含：`status`（`"success"` 或 `"failed"`）、`exit_code`、`stdout`、`stderr`、`result`（解析后的 result.json）。

`OpenCodeAgent` 实现使用 Python PTY 封装来捕获 TUI 输出，并自动响应权限提示，通过心跳守护进程向 `.heartbeat` 文件写入单调递增的序列号用于卡死检测。

#### MemoryManager

构建滑动窗口上下文，注入到 worker 的 prompt 中，基于 task 的 `topic` 和 `keywords`。按 topic 目录维护 `.index.md`。任务完成时写入记忆记录；分发时读取近期记录以提供跨任务记忆。

#### Notifier

当任务进入 `Pending Review` 时发送通知。实现可以是写入 inbox 目录、POST 到 webhook 或集成消息平台。回调：`on_approved` 和 `on_revision` 允许 Supervisor 对人工决策作出反应。

---

## 4. 任务状态机

```
                    ┌──────────────────────────────────────────┐
                    │                                          │
                    ▼                                          │
┌──────┐     ┌───────────┐    worker 启动    ┌────────────┐ │
│ Todo │────▶│ Claimed   │─────────────────▶│ In Progress│ │
└──────┘     └───────────┘                  └─────┬──────┘ │
                    │                                  │       │
                    │ 分发失败                          │ 完成  │
                    │ 或重试                           │       │
                    ▼                                  ▼       │
              ┌───────────┐                     ┌────────────┐  │
              │ Retry Q   │                     │Pending Rev.│  │
              │(指数退避) │                     └─────┬──────┘  │
              └───────────┘                          │         │
                    │                     ┌───────────┴─────────┤
                    │ 重试到期             │                   │
                    └─────────────────────┘                   │
                                              │  审批通过   ┌────┴───┐
                                              │───────────▶│  Done  │
                                              │            └────────┘
                                              │
                                              │  打回重做  ┌────────┐
                                              │───────────▶│  Todo  │
                                              │            └────────┘
                                              │
                                              │ 最大重试次数┌──────────┐
                                              │───────────▶│ Canceled │
                                                           └──────────┘
```

**状态说明：**

| 状态 | 含义 |
|------|------|
| `Todo` | 可分发 |
| `In Progress` | Worker 正在运行 |
| `Pending Review` | Worker 已完成，等待人工审批 |
| `Done` | 已审批并清理 |
| `Canceled` | 永久失败或放弃 |

**启动对账：** 每次启动，Supervisor 从 `tasks.json` 重建内存状态。对于每个 `In Progress` 任务：
- 若 `lock_pid` 进程仍存活 → 恢复到 `running{}` 继续监控。
- 若进程已死 → 重置为 `Todo`，`attempt_count++`。

---

## 5. 配置参考

所有路径均为**配置驱动**，无硬编码。完整示例见 `EXAMPLES/opencode_centric/configs/config.yaml`。

```yaml
tracker:
  kind: "jsonfile"
  tasks_file: "./symphony_data/tasks.json"

polling:
  interval_ms: 30000

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
  after_create: ""
  before_run: ""
  after_run: ""
  before_remove: ""
  timeout_ms: 60000

agent:
  executor: "opencode"
  opencode:
    command: "opencode --agent"
    max_turns: 20
  max_concurrent_agents: 3
  max_retry_backoff_ms: 300000

timeouts:
  read_timeout_ms: 5000
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000

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

## 6. 任务 Metadata 参考

Task metadata 是自由键值对，部分键有特殊含义：

| 键 | 类型 | 说明 |
|----|------|------|
| `external_files` | `list[dict]` | Worker 启动前复制到 workspace 的文件。格式：`{original: str, workspace_copy: str}` |
| `output_file` | `str` | 成功后将文件复制到 inbox 目录 |
| `verify_after_approve` | `bool` | 审批后运行自动验证 |
| `topic` | `str` | 记忆上下文的主题 |
| `keywords` | `list[str]` | 记忆过滤的关键词 |
| `repo_url` | `str` | 可用于 `after_create` 钩子脚本 |

---

## 7. 替换 Agent 运行时

OpenCode 是参考实现，不是唯一选项。换用不同 Agent 只需三步：

### 7.1 实现 AgentAdapter 接口

```python
from dataclasses import dataclass
from typing import Protocol

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

class AgentAdapter(Protocol):
    def start_session(self, workspace: "Workspace", prompt: str) -> Session: ...
    def wait_session(self, session: Session) -> AgentResult: ...
    def stop_session(self, session: Session) -> None: ...
    def is_process_alive(self, pid: int) -> bool: ...
```

### 7.2 更新 config.yaml

```yaml
agent:
  executor: "your_agent"
  your_agent:
    command: "your-agent --mode cli"
    max_turns: 20
```

### 7.3 在 symphony_core.py 中注册适配器

在 `start()` 方法中添加你的适配器导入和初始化：

```python
from your_agent_adapter import YourAgent
self.agent_adapter = YourAgent(self.config)
```

### 7.4 已知兼容 Agent

| Agent | 适配复杂度 | 备注 |
|-------|-----------|------|
| **OpenCode** | 已提供 | 参考实现，含 PTY 封装和权限自动应答 |
| **Claude Code** | 中等 | 需 `--print` 或结构化输出模式；无原生权限 API |
| **OpenAI Codex** | 中等 | CLI 模式与 OpenCode 类似 |
| **自定义脚本** | 低 | 任何读取 `prompt.txt` 并写入 `result.json` 的脚本均可 |

---

## 8. 外部文件沙盒详解

外部文件沙盒允许 Agent 修改生产文件而不直接操作它们。

### 流程

```
before_run 钩子               agent 运行              审批
    │                            │                   │
    ▼                            ▼                   ▼
复制外部文件            agent 在 workspace           将副本
到 workspace           中编辑副本（不知有原件）      写回到原路径
```

### 示例任务

```json
{
  "id": "TASK-FIX-AUTH",
  "title": "修复认证 Bug",
  "description": "修改 auth.py 中的登录函数，处理过期 Token。",
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

Agent 只在 workspace 中看到 `auth.py`。审批后，Supervisor 将修改后的 `auth.py` 复制回 `/path/to/production/auth.py`。

---

## 9. 启动对账算法

每次启动时，Supervisor 从 `tasks.json` 重建内存状态：

```
1. 扫描所有 state = "In Progress" 的任务
2. 对每个任务：
   a. 若 lock_pid 为空 → 重置为 Todo（崩溃在获取锁之前）
   b. 若 lock_pid 进程存活 → 恢复到 running{}，保持 attempt_count
   c. 若 lock_pid 进程已死 → 重置为 Todo，attempt_count++
3. 扫描所有 is_retrying = true 的任务：
   a. 若 retry_after 已到期 → 清除 is_retrying，加入分发池
   b. 若 retry_after 未来 → 重建内存重试条目
```

崩溃透明化。无任务静默消失；每次失败都有记录且可恢复。

---

## 10. 关键不变量与安全属性

1. **单实例强制：** 文件锁（`.instance.lock`）防止多个 Supervisor 进程同时运行。
2. **原子状态写入：** 所有 `tasks.json` 写操作使用 rename-from-temp + 独占文件锁。
3. **Attempt count 单调性：** `attempt_count` 永不减少，即使重置。
4. **黑名单持久化：** 被拉黑的任务写入 `tasks.json`，Supervisor 重启后依然生效。
5. **宽限期：** 新分发的任务有宽限期（默认：60s），心跳卡死检测在此期间不触发。
6. **禁止软链接 workspace：** workspace 创建时若检测到软链接则失败，防止路径遍历攻击。
