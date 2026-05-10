"""
Symphony-Lite 工具函数
FRAMEWORK/utils.py
"""
import os
import re
from pathlib import Path


def sanitize_workspace_name(task_id: str) -> str:
    """
    清理 task_id 中的非法字符，生成合法的目录名。
    只允许 [A-Za-z0-9._-]
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", task_id)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_-")
    if not sanitized:
        sanitized = "task"
    return sanitized


def enforce_workspace_invariant(workspace_path: str, workspace_root: str) -> None:
    """
    不变量1 + 不变量2：
    1. workspace_path 必须以 workspace_root 为前缀（防止 .. 逃逸）
    2. 目录名只允许 [A-Za-z0-9._-]

    抛出 SecurityError 如果不满足。
    """
    root = os.path.abspath(workspace_root)
    abs_path = os.path.abspath(workspace_path)

    if not abs_path.startswith(root + os.sep) and abs_path != root:
        raise SecurityError(f"Workspace path escapes root: {abs_path} (root={root})")

    dirname = os.path.basename(abs_path)
    if not re.match(r"^[A-Za-z0-9._-]+$", dirname):
        raise SecurityError(f"Invalid workspace directory name: {dirname}")


def get_hermes_home() -> Path:
    """
    获取当前 Hermes 实例的 HERMES_HOME。
    用于拼接 symphony 根目录。
    """
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home)
    return Path.home() / ".hermes"


def symphony_root() -> Path:
    """Symphony-Lite 根目录"""
    return get_hermes_home() / "symphony"


def workspace_root() -> Path:
    """工作区根目录"""
    return symphony_root() / "workspace_root"


class SecurityError(Exception):
    """工作区安全检查失败时抛出"""
    pass
