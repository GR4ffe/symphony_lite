"""
Symphony-Lite 配置加载器
/workspace/symphony/config_loader.py

加载并验证 config.yaml，提供类型化 getter。
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class ConfigError(Exception):
    """配置加载或验证错误"""


@dataclass
class TrackerConfig:
    kind: str = "jsonfile"
    tasks_file: str = "./symphony_data/tasks.json"
    active_states: list[str] = field(default_factory=lambda: ["Todo", "In Progress"])
    terminal_states: list[str] = field(default_factory=lambda: ["Done", "Canceled"])


@dataclass
class PollingConfig:
    interval_ms: int = 30_000


@dataclass
class WorkspaceConfig:
    root: str = "./symphony_data/workspace_root"


@dataclass
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60_000


@dataclass
class OpenCodeConfig:
    command: str = "opencode --agent"
    max_turns: int = 20
    pty_wrapper_script: str = "../../EXAMPLES/opencode_centric/opencode_pty_wrapper.py"


@dataclass
class AgentConfig:
    executor: str = "opencode"
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    max_concurrent_agents: int = 3
    max_retry_backoff_ms: int = 300_000


@dataclass
class TimeoutsConfig:
    read_timeout_ms: int = 5_000
    turn_timeout_ms: int = 3_600_000      # 1 hour
    stall_timeout_ms: int = 300_000        # 5 minutes


@dataclass
class MemoryConfig:
    constitution_file: str = "./symphony_data/constitution.md"
    index_root: str = "./symphony_data/memory_index"
    record_template: str = "{index_root}/{topic}/{date}-{slug}.md"
    index_filename: str = ".index.md"


@dataclass
class NotificationConfig:
    sillack_web_url: str | None = None
    inbox_dir: str = "./symphony_data/inbox"
    enabled: bool = True


@dataclass
class SandboxConfig:
    enabled: bool = True
    backup_orig: bool = True
    audit_log: bool = True


@dataclass
class VerifyConfig:
    auto_enabled: bool = False
    threshold: int = 80


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./symphony_data/logs/symphony.log"
    max_size_mb: int = 10


@dataclass
class Config:
    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    timeouts: TimeoutsConfig
    memory: MemoryConfig
    logging: LoggingConfig
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)

    # 原始字典（用于 render_hook 渲染）
    _raw: dict = field(default_factory=dict)

    @staticmethod
    def load(config_path: str) -> "Config":
        """从 YAML 文件加载配置，应用默认值"""
        if yaml is None:
            raise ConfigError("PyYAML is required: pip install pyyaml")

        if not os.path.exists(config_path):
            raise ConfigError(f"Config file not found: {config_path}")

        raw = yaml.safe_load(open(config_path))
        if raw is None:
            raw = {}

        return Config._from_dict(raw)

    @staticmethod
    def _from_dict(raw: dict) -> "Config":
        """从字典构建 Config 对象，带默认值填充"""
        tracker_raw = raw.get("tracker", {})
        tracker = TrackerConfig(
            kind=tracker_raw.get("kind", "jsonfile"),
            tasks_file=tracker_raw.get("tasks_file", "./symphony_data/tasks.json"),
            active_states=tracker_raw.get("active_states", ["Todo", "In Progress"]),
            terminal_states=tracker_raw.get("terminal_states", ["Done", "Canceled"]),
        )

        polling = PollingConfig(
            interval_ms=raw.get("polling", {}).get("interval_ms", 30_000),
        )

        workspace = WorkspaceConfig(
            root=raw.get("workspace", {}).get("root", "./symphony_data/workspace_root"),
        )

        hooks_raw = raw.get("hooks", {})
        hooks = HooksConfig(
            after_create=hooks_raw.get("after_create"),
            before_run=hooks_raw.get("before_run"),
            after_run=hooks_raw.get("after_run"),
            before_remove=hooks_raw.get("before_remove"),
            timeout_ms=hooks_raw.get("timeout_ms", 60_000),
        )

        agent_raw = raw.get("agent", {})
        opencode_raw = agent_raw.get("opencode", {})
        opencode = OpenCodeConfig(
            command=opencode_raw.get("command", "opencode --agent"),
            max_turns=opencode_raw.get("max_turns", 20),
            pty_wrapper_script=opencode_raw.get("pty_wrapper_script", "../../EXAMPLES/opencode_centric/opencode_pty_wrapper.py"),
        )
        agent = AgentConfig(
            executor=agent_raw.get("executor", "opencode"),
            opencode=opencode,
            max_concurrent_agents=agent_raw.get("max_concurrent_agents", 3),
            max_retry_backoff_ms=agent_raw.get("max_retry_backoff_ms", 300_000),
        )

        timeouts_raw = raw.get("timeouts", {})
        timeouts = TimeoutsConfig(
            read_timeout_ms=timeouts_raw.get("read_timeout_ms", 5_000),
            turn_timeout_ms=timeouts_raw.get("turn_timeout_ms", 3_600_000),
            stall_timeout_ms=timeouts_raw.get("stall_timeout_ms", 300_000),
        )

        memory_raw = raw.get("memory", {})
        memory = MemoryConfig(
            constitution_file=memory_raw.get("constitution_file", "./symphony_data/constitution.md"),
            index_root=memory_raw.get("index_root", "./symphony_data/memory_index"),
            record_template=memory_raw.get("record_template", "{index_root}/{topic}/{date}-{slug}.md"),
            index_filename=memory_raw.get("index_filename", ".index.md"),
        )

        logging_raw = raw.get("logging", {})
        logging_cfg = LoggingConfig(
            level=logging_raw.get("level", "INFO"),
            file=logging_raw.get("file", "./symphony_data/logs/symphony.log"),
            max_size_mb=logging_raw.get("max_size_mb", 10),
        )

        notif_raw = raw.get("notification", {})
        notif_cfg = NotificationConfig(
            sillack_web_url=notif_raw.get("sillack_web_url"),
            inbox_dir=notif_raw.get("inbox_dir", "./symphony_data/inbox"),
            enabled=notif_raw.get("enabled", True),
        )

        sandbox_raw = raw.get("sandbox", {})
        sandbox_cfg = SandboxConfig(
            enabled=sandbox_raw.get("enabled", True),
            backup_orig=sandbox_raw.get("backup_orig", True),
            audit_log=sandbox_raw.get("audit_log", True),
        )

        verify_raw = raw.get("verify", {})
        verify_cfg = VerifyConfig(
            auto_enabled=verify_raw.get("auto_enabled", False),
            threshold=verify_raw.get("threshold", 80),
        )

        return Config(
            tracker=tracker,
            polling=polling,
            workspace=workspace,
            hooks=hooks,
            agent=agent,
            timeouts=timeouts,
            memory=memory,
            logging=logging_cfg,
            notification=notif_cfg,
            sandbox=sandbox_cfg,
            verify=verify_cfg,
            _raw=raw,
        )

    def get_workspace_path(self, task_id: str) -> str:
        """返回 task_id 对应的绝对工作区路径"""
        import re
        # 清理非法字符
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", task_id)
        safe = re.sub(r"_+", "_", safe).strip("_-") or "task"
        return os.path.join(self.workspace.root, safe)

    def render_hook(self, hook_name: str, context: dict) -> str:
        """
        渲染钩子脚本，支持 {{ variable }} 插值。
        示例：
          render_hook("after_run", {"workspace_path": "/path", "task_id": "TASK-001"})
        """
        hook_script = getattr(self.hooks, hook_name, None)
        if not hook_script:
            return ""

        # 简单插值：{{ key }}
        result = hook_script
        for key, value in context.items():
            placeholder = "{{ " + key + " }}"
            result = result.replace(placeholder, str(value))
            # 也支持 {{key}}
            placeholder2 = "{{" + key + "}}"
            result = result.replace(placeholder2, str(value))

        return result
