"""
Symphony-Lite Test Fixtures

共享 fixture 供所有测试文件使用。
"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    """临时目录，测试结束后自动清理"""
    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        yield Path(tmp)
        os.chdir(old_cwd)


@pytest.fixture
def sample_config_dict():
    """一个典型的配置字典，供 Config._from_dict 使用"""
    return {
        "tracker": {
            "kind": "jsonfile",
            "tasks_file": "/tmp/tasks.json",
            "active_states": ["Todo", "In Progress"],
            "terminal_states": ["Done", "Canceled"],
        },
        "polling": {"interval_ms": 5000},
        "workspace": {"root": "/tmp/workspace_root"},
        "hooks": {
            "after_create": "echo 'created {{ task_id }}'",
            "before_run": None,
            "after_run": None,
            "before_remove": None,
            "timeout_ms": 30000,
        },
        "agent": {
            "executor": "opencode",
            "opencode": {
                "command": "opencode --agent",
                "max_turns": 10,
                "pty_wrapper_script": "/tmp/wrapper.py",
            },
            "max_concurrent_agents": 2,
            "max_retry_backoff_ms": 60000,
        },
        "timeouts": {
            "read_timeout_ms": 3000,
            "turn_timeout_ms": 600000,
            "stall_timeout_ms": 120000,
        },
        "memory": {
            "constitution_file": "/tmp/constitution.md",
            "index_root": "/tmp/memory_index",
            "record_template": "{index_root}/{topic}/{date}-{slug}.md",
            "index_filename": ".index.md",
        },
        "logging": {
            "level": "DEBUG",
            "file": "/tmp/symphony.log",
            "max_size_mb": 5,
        },
        "notification": {
            "sillack_web_url": "http://localhost:8001",
            "inbox_dir": "/tmp/inbox",
            "enabled": True,
        },
        "sandbox": {
            "enabled": True,
            "backup_orig": True,
            "audit_log": True,
        },
        "verify": {
            "auto_enabled": False,
            "threshold": 80,
        },
    }
