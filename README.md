# Symphony-Lite

> A lightweight, state-driven framework for orchestrating multiple AI coding agents in coordinated workflows.

**轻量级、状态驱动的多 Agent 编排框架，适用于协调式 AI 工作流。**

---

## What Is This? / 这是什么？

Symphony-Lite applies the **Supervisor-Worker** pattern to autonomous task execution. A lightweight orchestrator manages task lifecycle, delegates to pluggable agent runtimes, and enforces safety boundaries — enabling unattended operation with human oversight at every critical decision.

Symphony-Lite 将 **Supervisor-Worker** 模式应用于自主任务执行。轻量级编排器管理任务生命周期，委托给可插拔的 Agent 运行时，并执行安全边界——实现无人值守运行，同时在每个关键决策点保留人工监督。

```
tasks.json → [SymphonyOrchestrator] → [Agent Worker] → workspace/
                                  ↑ tick loop
                                  ↑ heartbeat monitoring
                                  ↑ retry with backoff
```

---

## Key Features / 核心特性

| Feature | Description |
|---------|-------------|
| **State-driven lifecycle** | `Todo → In Progress → Pending Review → Done/Canceled` |
| **Crash recovery** | Startup reconciliation rebuilds memory state from `tasks.json` |
| **External File Sandbox** | Agents modify copies; changes reviewed before write-back |
| **Pluggable agents** | OpenCode, Claude Code, Codex, or any CLI-capable agent |
| **Exponential retry** | Backoff with monotonic attempt count and hard cap |
| **Memory injection** | Cross-task context from past executions |

---

## Quick Start / 快速开始

```bash
# 1. Clone
git clone https://github.com/GR4ffe/symphony_lite.git
cd symphony_lite

# 2. Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # if you have one

# 3. Configure
cp EXAMPLES/opencode_centric/configs/config.yaml.example EXAMPLES/opencode_centric/configs/config.yaml
# edit config.yaml — set your paths and agent command

# 4. Run
python3 EXAMPLES/opencode_centric/scripts/run_orchestrator.py
```

See [DEPLOYMENT.md](DEPLOYMENT.md) (English) / [DEPLOYMENT_zh.md](DEPLOYMENT_zh.md)（中文）for a phased walkthrough.

---

## Project Structure / 项目结构

```
symphony_lite/
├── FRAMEWORK/                      # Framework core (agent-agnostic)
│   ├── symphony_core.py              # Tick loop + orchestrator
│   ├── tasks_db.py                  # Task state persistence
│   ├── workspace_mgr.py              # Workspace lifecycle
│   ├── config_loader.py              # Config loading
│   ├── memory_manager.py             # Cross-task memory
│   ├── notifier.py                  # Approval/revision notifications
│   ├── file_watcher.py              # External task file monitoring
│   └── ...
├── EXAMPLES/
│   └── opencode_centric/             # Reference implementation
│       ├── agent_adapter.py          # OpenCode adapter (AgentAdapter interface)
│       ├── opencode_pty_wrapper.py  # PTY wrapper + permission auto-response
│       ├── configs/
│       │   └── config.yaml          # Example configuration
│       └── scripts/
│           └── run_orchestrator.py  # Launcher
├── SPEC.md                          # Architecture specification (English)
├── SPEC_zh.md                       # 架构规范（中文）
├── DEPLOYMENT.md                    # Phased deployment guide (English)
├── DEPLOYMENT_zh.md                 # 分步部署指南（中文）
└── README.md                        # This file
```

---

## Documentation / 文档

| Document | Language | Content |
|----------|----------|---------|
| [SPEC.md](SPEC.md) | English | Architecture, components, state machine, API |
| [SPEC_zh.md](SPEC_zh.md) | 中文 | 架构、组件、状态机、API |
| [DEPLOYMENT.md](DEPLOYMENT.md) | English | 6-phase deployment guide |
| [DEPLOYMENT_zh.md](DEPLOYMENT_zh.md) | 中文 | 六阶段部署指南 |

---

## Switching Agents / 切换 Agent

OpenCode is one possible Worker. The `AgentAdapter` interface is agent-agnostic:

```python
# EXAMPLES/opencode_centric/agent_adapter.py is a reference implementation.
# To use Claude Code instead, implement the same interface:
class AgentAdapter(Protocol):
    def start_session(self, workspace: Workspace, prompt: str) -> Session: ...
    def wait_session(self, session: Session) -> AgentResult: ...
    def stop_session(self, session: Session) -> None: ...
    def is_process_alive(self, pid: int) -> bool: ...
```

See Phase 6 of [DEPLOYMENT.md](DEPLOYMENT.md) for the full procedure.

---

## License / 许可证

MIT License. See [LICENSE](LICENSE) for details.
