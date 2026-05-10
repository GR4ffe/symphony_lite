"""
Test suite for FRAMEWORK/utils.py

Covers:
- sanitize_workspace_name() — all edge cases
- enforce_workspace_invariant() — valid, escape, invalid dirname
- get_hermes_home() — with/without env var
- symphony_root(), workspace_root() — path composition
- SecurityError
"""

import os
from pathlib import Path

import pytest

from FRAMEWORK.utils import (
    sanitize_workspace_name,
    enforce_workspace_invariant,
    get_hermes_home,
    symphony_root,
    workspace_root,
    SecurityError,
)


# ── sanitize_workspace_name ─────────────────────────────────────


class TestSanitizeWorkspaceName:
    def test_basic_alphanumeric(self):
        """标准 task_id 不变"""
        assert sanitize_workspace_name("TASK-001") == "TASK-001"

    def test_with_dots_and_underscores(self):
        """点和下划线保留"""
        assert sanitize_workspace_name("my.task_1") == "my.task_1"

    def test_special_chars_to_underscore(self):
        """特殊字符变成下划线"""
        assert sanitize_workspace_name("TASK#001/foo") == "TASK_001_foo"

    def test_multiple_underscores_collapsed(self):
        """连续下划线合并为一个"""
        assert sanitize_workspace_name("a!!!b???c") == "a_b_c"

    def test_leading_trailing_dash_stripped(self):
        """前后 _- 被 strip"""
        assert sanitize_workspace_name("__TASK-001__") == "TASK-001"

    def test_all_invalid_fallback(self):
        """全是非法字符 → fallback 'task'"""
        assert sanitize_workspace_name("!!!") == "task"

    def test_empty_string_fallback(self):
        """空字符串 → fallback 'task'"""
        assert sanitize_workspace_name("") == "task"

    def test_only_dash(self):
        """纯短横线 → fallback 'task'"""
        assert sanitize_workspace_name("---") == "task"

    def test_unicode_chars(self):
        """Unicode 字符被替换"""
        result = sanitize_workspace_name("中文-TASK")
        assert result == "_TASK" or result == "TASK"

    def test_spaces_to_underscore(self):
        """空格变成下划线"""
        assert sanitize_workspace_name("my task") == "my_task"


# ── enforce_workspace_invariant ─────────────────────────────────


class TestEnforceWorkspaceInvariant:
    def test_valid_path_no_error(self):
        """合法路径不抛异常"""
        # enforce_workspace_invariant 检查绝对路径
        enforce_workspace_invariant("/workspace/TASK-001", "/workspace")

    def test_valid_path_equal_to_root(self):
        """路径等于根目录也合法"""
        enforce_workspace_invariant("/workspace", "/workspace")

    def test_path_escape_raises(self):
        """.. 逃逸抛出 SecurityError"""
        with pytest.raises(SecurityError, match="escapes root"):
            enforce_workspace_invariant("/workspace/../../etc", "/workspace")

    def test_invalid_dirname_raises(self):
        """目录名含非法字符（如空格）抛出 SecurityError"""
        with pytest.raises(SecurityError, match="Invalid workspace directory name"):
            enforce_workspace_invariant("/workspace/in valid", "/workspace")

    def test_root_trailing_slash_consistency(self):
        """root 末尾有无斜杠不影响"""
        # 都应当正常工作
        enforce_workspace_invariant("/workspace/TASK-001", "/workspace/")

    def test_symlink_in_path_but_path_contained(self):
        """路径本身在 root 内（不管它是不是 symlink）"""
        enforce_workspace_invariant("/workspace/sub/task1", "/workspace")


# ── get_hermes_home ─────────────────────────────────────────────


class TestGetHermesHome:
    def test_env_var_set(self, monkeypatch):
        """HERMES_HOME 设置时返回该路径"""
        monkeypatch.setenv("HERMES_HOME", "/custom/hermes")
        assert get_hermes_home() == Path("/custom/hermes")

    def test_env_var_not_set(self, monkeypatch):
        """HERMES_HOME 未设置时返回 ~/.hermes"""
        monkeypatch.delenv("HERMES_HOME", raising=False)
        result = get_hermes_home()
        assert result == Path.home() / ".hermes"


# ── symphony_root / workspace_root ────────────────────────────


class TestSymphonyRootPaths:
    def test_symphony_root_composition(self, monkeypatch):
        """symphony_root() 返回 <hermes_home>/symphony"""
        monkeypatch.setenv("HERMES_HOME", "/test/hermes")
        assert symphony_root() == Path("/test/hermes/symphony")

    def test_workspace_root_composition(self, monkeypatch):
        """workspace_root() 返回 <symphony_root>/workspace_root"""
        monkeypatch.setenv("HERMES_HOME", "/test/hermes")
        assert workspace_root() == Path("/test/hermes/symphony/workspace_root")


# ── SecurityError ────────────────────────────────────────────────


class TestSecurityError:
    def test_is_exception(self):
        """SecurityError 是 Exception 的子类"""
        assert issubclass(SecurityError, Exception)

    def test_can_be_raised_with_message(self):
        """可以带消息抛出"""
        with pytest.raises(SecurityError, match="test error"):
            raise SecurityError("test error")
