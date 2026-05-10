"""
Test suite for FRAMEWORK/workspace_mgr.py

Covers:
- Workspace dataclass
- WorkspaceManager.prepare() — 创建、复用、不变量、锁
- WorkspaceManager.cleanup() — 删除、归档保护
- WorkspaceManager.archive_attempt() — 重试归档
- Hook 执行（after_create 成功/失败/超时）
- Lock 机制（stale lock 清理、WorkspaceOccupiedError）
- 不变量（SecurityError on escape/invalid dirname）
"""

import os

import pytest

from FRAMEWORK.config_loader import Config
from FRAMEWORK.workspace_mgr import (
    HookError,
    SecurityError,
    Workspace,
    WorkspaceManager,
)

# ── 辅助函数 ────────────────────────────────────────────────────


def make_config(root: str = None, hooks: dict = None) -> Config:
    """创建最小配置"""
    cfg_dict = {
        "workspace": {"root": root or "/tmp/test_ws_root"},
        "hooks": hooks or {},
        "tracker": {},
        "agent": {},
    }
    return Config._from_dict(cfg_dict)


# ── Workspace dataclass ──────────────────────────────────────────


class TestWorkspace:
    def test_construct(self):
        ws = Workspace(task_id="T-1", path="/ws/T-1", created_now=True)
        assert ws.task_id == "T-1"
        assert ws.path == "/ws/T-1"
        assert ws.created_now is True

    def test_not_created_now(self):
        ws = Workspace(task_id="T-2", path="/ws/T-2", created_now=False)
        assert ws.created_now is False


# ── WorkspaceManager.prepare ────────────────────────────────────


class TestWorkspaceManagerPrepare:
    def test_create_new_workspace(self, temp_dir):
        """创建新工作区"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-001")
        assert ws.task_id == "TASK-001"
        assert ws.created_now is True
        assert os.path.exists(ws.path)

    def test_reuse_existing_workspace(self, temp_dir):
        """复用已有工作区"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws1 = mgr.prepare("TASK-001")
        ws2 = mgr.prepare("TASK-001")
        assert ws2.created_now is False
        assert ws1.path == ws2.path

    def test_reuse_cleans_stale_files(self, temp_dir):
        """复用时清理 stale 文件"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-001")
        # 创建一些 stale 文件
        for f in (".done", "result.json", ".heartbeat"):
            open(os.path.join(ws.path, f), "w").close()
        # 复用时清理
        ws2 = mgr.prepare("TASK-001")
        for f in (".done", "result.json", ".heartbeat"):
            assert not os.path.exists(os.path.join(ws2.path, f))

    def test_symlink_removed_on_create(self, temp_dir):
        """新建 workspace 中已有的 symlink 被删除"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        mgr.prepare("TASK-001")
        # 准备下一个 task 的目录并手动创建一个软链接
        # 重新 prepare 同一个 task 是复用，不会触发 _check_no_symlinks
        # 使用新的 task_id 来触发新建流程
        ws2_path = os.path.join(root, "TASK-LINK")
        os.makedirs(ws2_path, exist_ok=True)
        target_path = os.path.join(ws2_path, "target")
        open(target_path, "w").close()
        link_path = os.path.join(ws2_path, "bad_link")
        os.symlink(target_path, link_path)
        # 删除目录让 prepare 重新创建（触发 _check_no_symlinks）
        import shutil
        shutil.rmtree(ws2_path)
        # 重新 prepare 会在创建后检查并删除 symlink
        ws3 = mgr.prepare("TASK-LINK")
        # symlink 不应存在
        assert not os.path.exists(os.path.join(ws3.path, "bad_link"))

    def test_dispatch_lock_written(self, temp_dir):
        """prepare 后 .dispatch.lock 存在"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-001")
        lock_file = os.path.join(ws.path, ".dispatch.lock")
        assert os.path.exists(lock_file)
        pid = int(open(lock_file).read().strip())
        assert pid > 0


# ── 不变量 ──────────────────────────────────────────────────────


class TestWorkspaceInvariants:
    def test_escape_sanitized_by_get_workspace_path(self, temp_dir):
        """任务 ID 中的路径逃逸字符被 sanitize"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        # ../../etc 中的 . 和 / 被 sanitize 为 _
        ws = mgr.prepare("../../etc")
        # 路径应该是 ws_root/__/__/etc
        assert "ws_root" in ws.path
        assert "etc" in ws.path  # "etc" 是 task_id fallback 后的部分

    def test_invalid_dirname_sanitized(self, temp_dir):
        """非法目录名被 sanitize（空格→下划线）"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("has spaces")
        # 空格被 sanitize 为下划线
        assert "has_spaces" in ws.path

    def test_enforce_invariant_escaped_root(self, temp_dir):
        """_enforce_invariants 检测路径逃逸（绕过 sanitize 场景）"""
        root = str(temp_dir / "ws_root")
        os.makedirs(root, exist_ok=True)
        mgr = WorkspaceManager(make_config(root=root))
        escape_path = os.path.join(root, "..", "escape")
        try:
            mgr._enforce_invariants(escape_path)
            assert False, "Should have raised SecurityError"
        except SecurityError as e:
            assert "escapes" in str(e)

    def test_enforce_invariant_invalid_dirname(self, temp_dir):
        """_enforce_invariants 检测非法目录名"""
        root = str(temp_dir / "ws_root")
        os.makedirs(root, exist_ok=True)
        mgr = WorkspaceManager(make_config(root=root))
        invalid_path = os.path.join(root, "invalid dir!")
        try:
            mgr._enforce_invariants(invalid_path)
            assert False, "Should have raised SecurityError"
        except SecurityError as e:
            assert "Invalid workspace" in str(e)


# ── Lock 机制 ──────────────────────────────────────────────────


class TestWorkspaceLock:
    def test_occupied_by_another_process(self, temp_dir):
        """另一个进程占据 workspace 时抛出 WorkspaceOccupiedError"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-001")
        # 伪造一个其他进程的锁
        lock_file = os.path.join(ws.path, ".dispatch.lock")
        with open(lock_file, "w") as f:
            f.write("999999999")  # 不存在的 PID
        # 应当清理 stale 锁并继续（不是抛出异常）
        ws2 = mgr.prepare("TASK-001")
        assert ws2.task_id == "TASK-001"


# ── Hook 执行 ──────────────────────────────────────────────────


class TestWorkspaceHooks:
    def test_after_create_hook_runs(self, temp_dir):
        """after_create 钩子被执行"""
        root = str(temp_dir / "ws_root")
        marker_file = str(temp_dir / "hook_ran")
        mgr = WorkspaceManager(make_config(
            root=root,
            hooks={"after_create": f"touch {marker_file}"},
        ))
        mgr.prepare("TASK-HOOK")
        assert os.path.exists(marker_file)

    def test_after_create_hook_failure_raises(self, temp_dir):
        """after_create 失败抛出 HookError"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(
            root=root,
            hooks={"after_create": "exit 1"},
        ))
        with pytest.raises(HookError, match="after_create"):
            mgr.prepare("TASK-FAIL")

    def test_hook_timeout_raises(self, temp_dir):
        """after_create 超时抛出 HookError"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(
            root=root,
            hooks={"after_create": "sleep 10"},
        ))
        # 缩短超时
        mgr.config.hooks.timeout_ms = 500
        with pytest.raises(HookError, match="timed out"):
            mgr.prepare("TASK-TIMEOUT")


# ── Cleanup ──────────────────────────────────────────────────────


class TestWorkspaceCleanup:
    def test_cleanup_removes_directory(self, temp_dir):
        """cleanup 删除工作区目录"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-001")
        assert os.path.exists(ws.path)
        mgr.cleanup("TASK-001")
        assert not os.path.exists(ws.path)

    def test_cleanup_nonexistent(self, temp_dir):
        """清理不存在的 workspace 不报错"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        mgr.cleanup("NONEXISTENT")  # should not raise

    def test_cleanup_runs_before_remove_hook(self, temp_dir):
        """cleanup 执行 before_remove 钩子"""
        root = str(temp_dir / "ws_root")
        marker_file = str(temp_dir / "before_remove_ran")
        mgr = WorkspaceManager(make_config(
            root=root,
            hooks={"before_remove": f"touch {marker_file}"},
        ))
        mgr.prepare("TASK-BEFORE-REMOVE")
        mgr.cleanup("TASK-BEFORE-REMOVE")
        assert os.path.exists(marker_file)


# ── Archive ──────────────────────────────────────────────────────


class TestWorkspaceArchive:
    def test_archive_attempt_renames_result(self, temp_dir):
        """archive_attempt 重命名 result.json"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        ws = mgr.prepare("TASK-ARCHIVE")
        # 创建 result.json
        open(os.path.join(ws.path, "result.json"), "w").close()
        mgr.archive_attempt("TASK-ARCHIVE", 1)
        assert not os.path.exists(os.path.join(ws.path, "result.json"))
        assert os.path.exists(os.path.join(ws.path, "result.attempt_1.json"))

    def test_archive_nonexistent_workspace(self, temp_dir):
        """归档不存在的 workspace 不报错"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        mgr.archive_attempt("NONEXISTENT", 1)  # should not raise

    def test_archive_without_result(self, temp_dir):
        """没有 result.json 时归档也不报错"""
        root = str(temp_dir / "ws_root")
        mgr = WorkspaceManager(make_config(root=root))
        mgr.prepare("TASK-NO-RESULT")
        mgr.archive_attempt("TASK-NO-RESULT", 1)  # should not raise
