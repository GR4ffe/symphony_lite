"""
Symphony-Lite 工作区管理器
/workspace/symphony/workspace_mgr.py

职责：
- 创建/复用/删除工作区目录
- 执行钩子（after_create / before_run / after_run / before_remove）
- 强制执行工作区隔离三不变量
"""
import os
import re
import shutil
import subprocess
import fcntl
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import Config


@dataclass
class Workspace:
    task_id: str
    path: str
    created_now: bool


class HookError(Exception):
    """钩子执行失败（仅在 before_run/after_create 失败时抛出）"""
    pass


class WorkspaceOccupiedError(Exception):
    """工作区被另一个进程占用（FIX-004：禁止重复拉起）"""
    pass


class WorkspaceManager:
    """
    工作区管理器。

    三不变量：
    1. workspace_path 必须以 workspace_root 为前缀
    2. 目录名只允许 [A-Za-z0-9._-]
    3. 不允许软链接（after_create 时检查）
    """

    def __init__(self, config: "Config"):
        self.config = config
        # task_id → created_now 缓存（同一 tick 内可能多次访问）
        self._created_now_cache: dict[str, bool] = {}

    def prepare(self, task_id: str) -> Workspace:
        """
        1. 计算路径（sanitize task_id）
        2. 强制不变量1+2：路径必须在 workspace_root 内，目录名合法
        3. FIX-004：如果 workspace 已存在，检查锁文件里的 PID 是否还活着
           - PID 存活 → 抛出 WorkspaceOccupiedError（禁止重复拉起）
           - PID 已死 → 清理 stale 锁文件，继续
        4. 创建/复用工作区目录
        5. 如果是新建，写入 .dispatch.lock（FIX-004）
        6. 如果是新建，运行 after_create 钩子
        7. 返回 Workspace 对象
        """
        workspace_path = self.config.get_workspace_path(task_id)
        self._enforce_invariants(workspace_path)

        # FIX-004：清理 stale workspace 锁
        self._clean_stale_lock(workspace_path)

        created_now = not os.path.exists(workspace_path)
        if created_now:
            os.makedirs(workspace_path, exist_ok=True)
            self._run_hook("after_create", workspace_path, task_id)
            # 检查软链接
            self._check_no_symlinks(workspace_path)
        else:
            # Workspace 复用时：清理上一次 run 留下的 stale 文件
            # 防止旧 .done / result.json 导致 bash wrapper 误判并返回旧 exit_code
            for stale_file in (".done", "result.json", ".heartbeat"):
                p = os.path.join(workspace_path, stale_file)
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass

        # FIX-004：写入当前 PID 锁（无论新建还是复用都更新，因为可能刚清了 stale lock）
        self._write_dispatch_lock(workspace_path, os.getpid())

        self._created_now_cache[task_id] = created_now
        return Workspace(task_id=task_id, path=workspace_path, created_now=created_now)

    def _write_dispatch_lock(self, workspace_path: str, pid: int) -> None:
        """
        FIX-004: 向 workspace 写入当前 PID 锁文件。
        用于后续调用 prepare 时检测是否有其他进程正在使用该 workspace。
        """
        lock_file = os.path.join(workspace_path, ".dispatch.lock")
        try:
            with open(lock_file, "w") as f:
                f.write(str(pid))
        except IOError:
            # 锁文件写入失败不影响主流程，只是少了保护
            pass

    def _clean_stale_lock(self, workspace_path: str) -> None:
        """
        FIX-004: 检查 workspace 的 .dispatch.lock 文件。
        - 锁不存在 → 无事
        - PID 存活 → 抛出 WorkspaceOccupiedError（workspace 被其他进程占用）
        - PID 已死 → 删除锁文件，继续（允许重复调度）
        """
        lock_file = os.path.join(workspace_path, ".dispatch.lock")
        if not os.path.exists(lock_file):
            return

        try:
            with open(lock_file, "r") as f:
                pid = int(f.read().strip())
        except (ValueError, IOError):
            # 锁文件损坏，当作无锁处理
            try:
                os.remove(lock_file)
            except OSError:
                pass
            return

        # 检查 PID 是否存活（zombie 也是"死"的，不算占用）
        # 方案：尝试 os.kill(pid, 0)，对 zombie 不抛 ProcessLookupError，
        # 故额外读 /proc/PID/status 确认 State 不是 'Z'
        is_zombie = False
        try:
            state_file = f"/proc/{pid}/status"
            with open(state_file) as f:
                for line in f:
                    if line.startswith("State:"):
                        is_zombie = "Z" in line.split()[1]
                        break
        except (FileNotFoundError, IOError):
            # 进程不存在（立即清理）
            try:
                os.remove(lock_file)
            except OSError:
                pass
            return

        if is_zombie:
            # Zombie 当作"锁无效"处理（清理后继续）
            try:
                os.remove(lock_file)
            except OSError:
                pass
            return

        try:
            os.kill(pid, 0)  # 不发信号，只检测进程是否存在
            # 进程存活，检查是否和当前进程一样（同一进程重复调用 prepare 允许）
            if pid != os.getpid():
                raise WorkspaceOccupiedError(
                    f"Workspace {workspace_path} is locked by pid={pid} (another process)"
                )
        except ProcessLookupError:
            # PID 不存在，stale lock，清理
            try:
                os.remove(lock_file)
            except OSError:
                pass

    def cleanup(self, task_id: str) -> None:
        """
        1. 运行 before_remove 钩子
        2. 删除工作区目录
        """
        workspace_path = self.config.get_workspace_path(task_id)
        if not os.path.exists(workspace_path):
            return

        # I6：归档检查——如果有待清理的 attempt 文件，说明之前有重试归档，保留目录不删
        attempt_files = [
            f for f in os.listdir(workspace_path)
            if f.startswith("result.attempt_") or f.startswith("opencode.attempt_")
        ]
        if attempt_files:
            print(f"[WorkspaceManager] {task_id} has {len(attempt_files)} archived attempts, keeping workspace for audit")

        self._run_hook("before_remove", workspace_path, task_id)
        shutil.rmtree(workspace_path)

    def archive_attempt(self, task_id: str, attempt: int) -> None:
        """
        I6：将 workspace 中当前的结果文件归档为 result.attempt_{N}.json。
        重试时保留失败痕迹供 GRaffe 排查，但清理主 result.json 让新尝试有干净起点。
        """
        workspace_path = self.config.get_workspace_path(task_id)
        if not os.path.exists(workspace_path):
            return

        # 归档 result.json
        result_file = os.path.join(workspace_path, "result.json")
        if os.path.exists(result_file):
            archived = os.path.join(workspace_path, f"result.attempt_{attempt}.json")
            os.rename(result_file, archived)

        # 归档 opencode.log
        log_file = os.path.join(workspace_path, "opencode.log")
        if os.path.exists(log_file):
            archived_log = os.path.join(workspace_path, f"opencode.attempt_{attempt}.log")
            os.rename(log_file, archived_log)

    def run_hook(self, hook_name: str, workspace_path: str, task_id: str) -> None:
        """公开钩子执行入口（供 symphony_core 调用）"""
        self._run_hook(hook_name, workspace_path, task_id)

    def _enforce_invariants(self, workspace_path: str) -> None:
        """
        不变量1：workspace_path 必须以 workspace_root 为前缀
        不变量2：目录名只允许 [A-Za-z0-9._-]
        """
        root = os.path.abspath(self.config.workspace.root)
        abs_path = os.path.abspath(workspace_path)

        if not (abs_path.startswith(root + os.sep) or abs_path == root):
            raise SecurityError(f"Workspace path escapes root: {abs_path} (root={root})")

        dirname = os.path.basename(abs_path)
        if not re.match(r"^[A-Za-z0-9._-]+$", dirname):
            raise SecurityError(f"Invalid workspace directory name: {dirname}")

    def _check_no_symlinks(self, workspace_path: str) -> None:
        """
        安全检查：workspace 内不允许软链接（after_create 时检查）。
        发现则删除并警告。
        """
        for entry in os.scandir(workspace_path):
            if entry.is_symlink():
                os.remove(entry.path)
                # 从 logger 导入会有循环依赖问题，用 print 代替
                print(f"[WorkspaceManager] WARNING: removed symlink in workspace: {entry.path}")

    def _run_hook(self, hook_name: str, workspace_path: str, task_id: str) -> None:
        """执行钩子，超时则中止"""
        hook_script = getattr(self.config.hooks, hook_name, None)
        if not hook_script or not hook_script.strip():
            return

        context = {
            "workspace_path": workspace_path,
            "task_id": task_id,
        }
        rendered = self.config.render_hook(hook_name, context)

        if not rendered.strip():
            return

        try:
            result = subprocess.run(
                ["bash", "-lc", rendered],
                cwd=workspace_path,
                timeout=self.config.hooks.timeout_ms / 1000,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            if hook_name in ("after_create", "before_run"):
                raise HookError(f"{hook_name} timed out for {task_id} (> {self.config.hooks.timeout_ms}ms)")
            else:
                print(f"[WorkspaceManager] WARNING: {hook_name} timed out for {task_id}")
                return

        if result.returncode != 0:
            if hook_name in ("after_create", "before_run"):
                raise HookError(
                    f"{hook_name} failed for {task_id} (exit={result.returncode}): {result.stderr}"
                )
            else:
                print(f"[WorkspaceManager] WARNING: {hook_name} failed for {task_id}: {result.stderr}")

    # ── External File Sandbox ───────────────────────────────────────────────

    def _prepare_workspace(self, workspace_path: str, task_id: str, metadata: dict | None) -> None:
        """
        Phase 1: before_run 钩子之前调用。

        将 task.metadata.external_files 中的外部文件复制进 workspace，
        供 opencode 在 workspace 副本上编辑。
        写入 .orig 备份文件供 after_approve 写回时 diff 追溯。

        边界情况：
        - original 文件不存在 → WARNING 日志，task 继续 dispatch
        - metadata 为空或无 external_files → 空操作
        """
        external_files = metadata.get("external_files", []) if metadata else []
        if not external_files:
            return

        import json as _json
        audit_records = []

        for item in external_files:
            original = item.get("original", "")
            copy_name = item.get("workspace_copy", "")
            if not original or not copy_name:
                print(f"[WorkspaceManager] WARNING: malformed external_files entry for {task_id}: {item}")
                continue

            src = original
            dest = os.path.join(workspace_path, copy_name)
            orig_backup = dest + ".orig"

            if os.path.exists(src):
                # 展开 symlink（避免写入 link 本身）
                real_src = os.path.realpath(src)
                shutil.copy2(real_src, dest)
                # 备份原始版本到 .orig（永久保留）
                shutil.copy2(real_src, orig_backup)
                print(f"[WorkspaceManager] INFO: file_sandbox_injected {src} -> workspace/{copy_name}")
                audit_records.append({
                    "action": "injected",
                    "original": original,
                    "workspace_copy": copy_name,
                    "task_id": task_id,
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
            else:
                print(f"[WorkspaceManager] WARNING: file_sandbox_missing {original} (not found)")

        # 写审计日志
        if audit_records:
            audit_path = os.path.join(workspace_path, "_dispatch_audit.json")
            try:
                existing = []
                if os.path.exists(audit_path):
                    with open(audit_path) as f:
                        existing = _json.load(f)
                existing.extend(audit_records)
                with open(audit_path, "w") as f:
                    _json.dump(existing, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[WorkspaceManager] WARNING: failed to write _dispatch_audit.json: {e}")

    def deploy_sandboxed_files(
        self,
        workspace_path: str,
        task_id: str,
        metadata: dict | None,
        config: "Config | None" = None,
    ) -> None:
        """
        Phase 2: after_approve 钩子——把 workspace 中的修改写回 original 路径。

        边界情况：
        - workspace 文件不存在 → WARNING，跳过
        - mtime 或 checksum 与 .orig 相同 → 无修改，跳过写回（避免无效覆盖）
        - original 是 symlink → 展开后写回
        - 并发 approve → 加文件锁（flock）防重复
        """
        import json as _json

        external_files = metadata.get("external_files", []) if metadata else []
        if not external_files:
            return

        sandbox_cfg = {}
        if config:
            sandbox_cfg = getattr(config, "sandbox", {})

        backup_orig = sandbox_cfg.get("backup_orig", True)
        audit_records = []

        for item in external_files:
            original = item.get("original", "")
            copy_name = item.get("workspace_copy", "")
            if not original or not copy_name:
                continue

            ws_copy = os.path.join(workspace_path, copy_name)
            orig_backup = ws_copy + ".orig"

            if not os.path.exists(ws_copy):
                print(f"[WorkspaceManager] WARNING: file_sandbox_no_output {original} (workspace copy not found)")
                continue

            # 展开 symlink（避免写入 link 本身）
            real_original = os.path.realpath(original)
            dest_dir = os.path.dirname(real_original)

            # 检查是否有实质修改（mtime 或 checksum）
            has_change = False
            if os.path.exists(orig_backup):
                import filecmp
                if not filecmp.cmp(ws_copy, orig_backup, shallow=False):
                    has_change = True
            else:
                has_change = True  # 无 .orig 说明是全新注入，直接写回

            if not has_change:
                print(f"[WorkspaceManager] INFO: file_sandbox_unchanged {original}, skipping deploy")
                continue

            # 加文件锁防止并发写回
            lock_path = os.path.join(workspace_path, f".{copy_name}.deploy.lock")
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                print(f"[WorkspaceManager] WARNING: could not acquire deploy lock for {copy_name}, skipping")
                continue

            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(ws_copy, real_original)
                print(f"[WorkspaceManager] INFO: file_sandbox_deployed workspace/{copy_name} -> {original}")
                audit_records.append({
                    "action": "deployed",
                    "original": original,
                    "workspace_copy": copy_name,
                    "task_id": task_id,
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
            except Exception as e:
                print(f"[WorkspaceManager] ERROR: failed to deploy {original}: {e}")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                try:
                    os.remove(lock_path)
                except OSError:
                    pass

        # 写审计日志
        if audit_records:
            audit_path = os.path.join(workspace_path, "_dispatch_audit.json")
            try:
                existing = []
                if os.path.exists(audit_path):
                    with open(audit_path) as f:
                        existing = _json.load(f)
                existing.extend(audit_records)
                with open(audit_path, "w") as f:
                    _json.dump(existing, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[WorkspaceManager] WARNING: failed to write _dispatch_audit.json: {e}")


class SecurityError(Exception):
    """工作区安全检查失败时抛出"""
    pass
