"""
Symphony-Lite 结构化日志
FRAMEWORK/logger.py

所有日志条目格式：
  [HH:MM:SS] LEVEL  event=XXX  task_id=XXX  session_id=XXX  outcome=XXX  detail=XXX
"""
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler


def _fmt_extra(extra: dict) -> str:
    """将 dict 格式化为 key=value 对，过滤 None 值"""
    parts = []
    for k, v in extra.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return "  ".join(parts)


class SymphonyLogger:
    """
    结构化日志封装，输出到文件 + stdout。
    所有事件型日志使用 info()，带上 task_id/session_id/event/outcome。
    """

    def __init__(self, log_file: str, level: str = "INFO", max_size_mb: int = 10):
        self.logger = logging.getLogger("symphony")
        self.logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        self.logger.handlers.clear()

        # 文件 handler（轮转）
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = RotatingFileHandler(
            log_file,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=3,
            encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)

        # stdout handler
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)

        fmt = logging.Formatter(
            "[%(asctime)s]  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)

        self.logger.addHandler(fh)
        self.logger.addHandler(sh)

    def _log(self, level: str, event: str, task_id: str = None,
             session_id: str = None, outcome: str = None, detail: str = None):
        """统一日志入口"""
        extra = f"event={event}"
        if task_id:
            extra += f"  task_id={task_id}"
        if session_id:
            extra += f"  session_id={session_id}"
        if outcome:
            extra += f"  outcome={outcome}"
        if detail:
            extra += f"  detail={detail}"
        getattr(self.logger, level.lower())(extra)

    def info(self, event: str, task_id: str = None, session_id: str = None,
             outcome: str = None, detail: str = None):
        self._log("INFO", event, task_id, session_id, outcome, detail)

    def warning(self, event: str, task_id: str = None, session_id: str = None,
                outcome: str = None, detail: str = None):
        self._log("WARNING", event, task_id, session_id, outcome, detail)

    def error(self, event: str, task_id: str = None, session_id: str = None,
              outcome: str = None, detail: str = None):
        self._log("ERROR", event, task_id, session_id, outcome, detail)

    def debug(self, event: str, task_id: str = None, session_id: str = None,
              outcome: str = None, detail: str = None):
        self._log("DEBUG", event, task_id, session_id, outcome, detail)


# 全局实例（由 symphony_core 初始化时创建）
log: SymphonyLogger | None = None


def init_logger(log_file: str, level: str = "INFO", max_size_mb: int = 10) -> SymphonyLogger:
    global log
    log = SymphonyLogger(log_file=log_file, level=level, max_size_mb=max_size_mb)
    return log
