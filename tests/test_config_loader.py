"""
Test suite for FRAMEWORK/config_loader.py

Covers:
- Config._from_dict() — pure dict → Config construction
- Config.load() — YAML file loading
- Config.get_workspace_path() — path generation
- Config.render_hook() — template interpolation
- ConfigError
- All sub-config dataclasses default values
"""


import pytest

from FRAMEWORK.config_loader import (
    AgentConfig,
    Config,
    ConfigError,
    HooksConfig,
    LoggingConfig,
    OpenCodeConfig,
    PollingConfig,
    TrackerConfig,
    WorkspaceConfig,
)

# ── Sub-config dataclass defaults ────────────────────────────────


class TestTrackerConfigDefaults:
    def test_default_values(self):
        cfg = TrackerConfig()
        assert cfg.kind == "jsonfile"
        assert cfg.tasks_file == "./symphony_data/tasks.json"
        assert cfg.active_states == ["Todo", "In Progress"]
        assert cfg.terminal_states == ["Done", "Canceled"]

    def test_custom_values(self):
        cfg = TrackerConfig(kind="custom", active_states=["Pending"])
        assert cfg.kind == "custom"
        assert cfg.active_states == ["Pending"]


class TestPollingConfigDefaults:
    def test_default_interval(self):
        cfg = PollingConfig()
        assert cfg.interval_ms == 30_000


class TestWorkspaceConfigDefaults:
    def test_default_root(self):
        cfg = WorkspaceConfig()
        assert cfg.root == "./symphony_data/workspace_root"


class TestHooksConfigDefaults:
    def test_default_all_none(self):
        cfg = HooksConfig()
        assert cfg.after_create is None
        assert cfg.before_run is None
        assert cfg.after_run is None
        assert cfg.before_remove is None
        assert cfg.timeout_ms == 60_000


class TestOpenCodeConfigDefaults:
    def test_default_values(self):
        cfg = OpenCodeConfig()
        assert cfg.command == "opencode --agent"
        assert cfg.max_turns == 20


class TestAgentConfigDefaults:
    def test_default_values(self):
        cfg = AgentConfig()
        assert cfg.executor == "opencode"
        assert cfg.max_concurrent_agents == 3
        assert cfg.max_retry_backoff_ms == 300_000
        assert isinstance(cfg.opencode, OpenCodeConfig)


class TestLoggingConfigDefaults:
    def test_default_values(self):
        cfg = LoggingConfig()
        assert cfg.level == "INFO"
        assert cfg.max_size_mb == 10


# ── Config._from_dict ────────────────────────────────────────────


class TestConfigFromDict:
    def test_empty_dict_uses_defaults(self):
        """空字典使用所有默认值"""
        cfg = Config._from_dict({})
        assert cfg.tracker.kind == "jsonfile"
        assert cfg.polling.interval_ms == 30_000
        assert cfg.workspace.root == "./symphony_data/workspace_root"
        assert cfg.agent.executor == "opencode"
        assert cfg.agent.max_concurrent_agents == 3
        assert cfg.sandbox.enabled is True

    def test_partial_override(self):
        """部分覆盖，其余保持默认"""
        raw = {
            "tracker": {"kind": "redis"},
            "agent": {"max_concurrent_agents": 5},
        }
        cfg = Config._from_dict(raw)
        assert cfg.tracker.kind == "redis"
        # 其他仍为默认
        assert cfg.tracker.tasks_file == "./symphony_data/tasks.json"
        assert cfg.agent.max_concurrent_agents == 5
        assert cfg.agent.executor == "opencode"

    def test_full_config(self, sample_config_dict):
        """完整配置字典能正确解析所有字段"""
        cfg = Config._from_dict(sample_config_dict)
        assert cfg.tracker.kind == "jsonfile"
        assert cfg.tracker.tasks_file == "/tmp/tasks.json"
        assert cfg.polling.interval_ms == 5000
        assert cfg.workspace.root == "/tmp/workspace_root"
        assert cfg.hooks.after_create == "echo 'created {{ task_id }}'"
        assert cfg.agent.executor == "opencode"
        assert cfg.agent.opencode.max_turns == 10
        assert cfg.agent.max_concurrent_agents == 2
        assert cfg.timeouts.read_timeout_ms == 3000
        assert cfg.logging.level == "DEBUG"
        assert cfg.notification.sillack_web_url == "http://localhost:8001"
        assert cfg.sandbox.enabled is True
        assert cfg.verify.auto_enabled is False

    def test_raw_dict_preserved(self, sample_config_dict):
        """_raw 字段保留原始输入"""
        cfg = Config._from_dict(sample_config_dict)
        assert cfg._raw["tracker"]["kind"] == "jsonfile"

    def test_none_config_handling(self):
        """notification.sillack_web_url 可以为 None"""
        cfg = Config._from_dict({"notification": {}})
        assert cfg.notification.sillack_web_url is None


# ── Config.load ──────────────────────────────────────────────────


class TestConfigLoad:
    def test_load_from_yaml_file(self, temp_dir):
        """从 YAML 文件加载配置"""
        yaml_path = temp_dir / "config.yaml"
        yaml_path.write_text("""
tracker:
  kind: jsonfile
  tasks_file: /tmp/tasks.json
agent:
  max_concurrent_agents: 4
""")
        cfg = Config.load(str(yaml_path))
        assert cfg.tracker.kind == "jsonfile"
        assert cfg.agent.max_concurrent_agents == 4

    def test_load_empty_yaml(self, temp_dir):
        """空 YAML 文件使用所有默认值"""
        yaml_path = temp_dir / "empty.yaml"
        yaml_path.write_text("")
        cfg = Config.load(str(yaml_path))
        assert cfg.tracker.kind == "jsonfile"

    def test_load_file_not_found(self):
        """不存在的文件抛出 ConfigError"""
        with pytest.raises(ConfigError, match="not found"):
            Config.load("/nonexistent/path.yaml")


# ── Config.get_workspace_path ────────────────────────────────────


class TestGetWorkspacePath:
    def test_basic_path(self):
        """标准 task_id 生成正确路径"""
        cfg = Config._from_dict({"workspace": {"root": "/workspace"}})
        path = cfg.get_workspace_path("TASK-001")
        assert path == "/workspace/TASK-001"

    def test_special_chars_sanitized(self):
        """特殊字符被清理"""
        cfg = Config._from_dict({"workspace": {"root": "/ws"}})
        path = cfg.get_workspace_path("TASK#001/foo")
        assert "TASK_001_foo" in path

    def test_all_invalid_fallback(self):
        """全是非法字符 fallback"""
        cfg = Config._from_dict({"workspace": {"root": "/ws"}})
        path = cfg.get_workspace_path("!!!")
        assert path == "/ws/task"


# ── Config.render_hook ────────────────────────────────────────────


class TestRenderHook:
    def test_basic_interpolation(self):
        """{{ variable }} 插值"""
        cfg = Config._from_dict({
            "hooks": {"after_create": "echo 'created {{ task_id }}'"}
        })
        result = cfg.render_hook("after_create", {"task_id": "TASK-001"})
        assert result == "echo 'created TASK-001'"

    def test_no_brackets_interpolation(self):
        """也支持 {{key}} 不带空格"""
        cfg = Config._from_dict({
            "hooks": {"after_create": "echo 'created {{task_id}}'"}
        })
        result = cfg.render_hook("after_create", {"task_id": "TASK-001"})
        assert result == "echo 'created TASK-001'"

    def test_multiple_variables(self):
        """多变量插值"""
        cfg = Config._from_dict({
            "hooks": {"after_run": "cp {{ src }} {{ dst }}"}
        })
        result = cfg.render_hook("after_run", {"src": "/a", "dst": "/b"})
        assert result == "cp /a /b"

    def test_none_hook_returns_empty(self):
        """未设置的 hook 返回空字符串"""
        cfg = Config._from_dict({})
        result = cfg.render_hook("after_create", {})
        assert result == ""


# ── ConfigError ──────────────────────────────────────────────────


class TestConfigError:
    def test_is_exception(self):
        assert issubclass(ConfigError, Exception)

    def test_custom_message(self):
        with pytest.raises(ConfigError, match="custom"):
            raise ConfigError("custom error message")
