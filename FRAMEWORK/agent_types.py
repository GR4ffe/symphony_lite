"""
Symphony-Lite Agent 类型定义
FRAMEWORK/agent_types.py

所有 Agent 适配器共享的接口和类型。不依赖具体 Agent 实现。
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_mgr import Workspace


@dataclass
class Session:
    """Agent 执行会话。由适配器 start_session() 返回，Supervisor 用其跟踪任务。"""
    id: str
    pid: int
    workspace: "Workspace | None" = None
    process: object = None  # subprocess.Popen-like


@dataclass
class AgentResult:
    """Agent 执行结果。由适配器 wait_session() 返回。"""
    status: str  # "success" | "failed"
    exit_code: int
    stdout: str
    stderr: str
    result: dict  # 从 result.json 解析


class AgentAdapter:
    """
    Agent 适配器基类。

    所有 Agent 运行时（OpenCode, Claude Code, Codex, ...）必须实现此接口。
    参考实现见 EXAMPLES/opencode_centric/agent_adapter.py
    """

    def start_session(self, workspace: "Workspace", prompt: str) -> Session:
        raise NotImplementedError

    def wait_session(self, session: Session) -> AgentResult:
        raise NotImplementedError

    def stop_session(self, session: Session) -> None:
        raise NotImplementedError

    def is_process_alive(self, pid: int) -> bool:
        raise NotImplementedError
