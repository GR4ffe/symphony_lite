"""
Symphony-Lite 通知管理器
FRAMEWORK/notifier.py

职责：
- 任务完成时通知 GRaffe（sillack-web inbox 写文件 或 HTTP POST）
- 审批结果监听（inotify 监控 tasks.json 变更）
"""
import json
import os
import time
from datetime import datetime, timezone
from threading import Thread
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import Config
    from tasks_db import Task


class Notifier:
    """
    GRaffe 通知器。

    通知方式（按优先级）：
    1. HTTP POST 到 sillack-web（如果 sillack_web_url 配置了）
    2. 写 JSON 通知文件到 inbox 目录

    审批监听：
    - 使用 inotify 监控 tasks.json mtime 变化
    - 发现 Pending Review → Done（GRaffe 验收通过）→ 回调 on_approved
    - 发现 Pending Review → Todo（GRaffe 打回重做）→ 回调 on_revision
    """

    def __init__(self, config: "Config"):
        self.config = config
        self.sillack_web_url = getattr(config, "sillack_web_url", None) or os.getenv(
            "SILLACK_WEB_URL", "http://localhost:8001"
        )
        self.inbox_dir = getattr(config, "inbox_dir", None) or "./symphony_data/inbox"
        self._listeners: list[Callable] = []
        self._running = False
        self._listener_thread: Thread | None = None

    def start_listening(self, tasks_db) -> None:
        """
        启动审批监听线程。
        使用轮询（兼容 WSL/inotify 不可用环境），每 10s 检查一次 tasks.json。
        检测到 Pending Review 任务状态变更时触发回调。
        """
        self._running = True
        self._listener_thread = Thread(
            target=self._listen_loop,
            args=(tasks_db,),
            name="symphony-approval-listener",
            daemon=True,
        )
        self._listener_thread.start()

    def stop_listening(self) -> None:
        self._running = False
        if self._listener_thread:
            self._listener_thread.join(timeout=5)

    def on_approved(self, callback: Callable[["Task"], None]) -> None:
        """注册验收通过回调（GRaffe 把 Pending Review → Done）"""
        self._listeners.append(("approved", callback))

    def on_revision(self, callback: Callable[["Task", str], None]) -> None:
        """注册打回重做回调（GRaffe 把 Pending Review → Todo，并可能带理由）"""
        self._listeners.append(("revision", callback))

    def _notify(self, task: "Task", event_type: str) -> None:
        """发送通知"""
        payload = {
            "type": "symphony_task_notification",
            "event": event_type,
            "task_id": task.id,
            "title": task.title,
            "topic": task.topic,
            "workspace": self.config.get_workspace_path(task.id),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "result": task.result,
        }

        # 方式1: HTTP POST
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{self.sillack_web_url}/api/symphony/notify",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(f"[Notifier] Sent HTTP notification for {task.id}")
                    return
        except Exception as e:
            print(f"[Notifier] HTTP notification failed ({e}), falling back to file")

        # 方式2: 写 inbox 文件
        self._write_inbox_file(task, payload)

    def _write_inbox_file(self, task: "Task", payload: dict) -> None:
        """写通知文件到 inbox 目录"""
        os.makedirs(self.inbox_dir, exist_ok=True)
        filename = f"{task.id}_{payload['event']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        path = os.path.join(self.inbox_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"[Notifier] Wrote inbox notification: {path}")

    def notify_task_pending_review(self, task: "Task") -> None:
        """通知 GRaffe：任务完成，等待验收"""
        self._notify(task, "pending_review")

    def notify_task_retrying(self, task: "Task", attempt: int, error: str) -> None:
        """通知 GRaffe：任务失败，正在重试"""
        payload = {
            "type": "symphony_task_notification",
            "event": "retrying",
            "task_id": task.id,
            "title": task.title,
            "attempt": attempt,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        # 重试不打扰 GRaffe，只写日志，不发通知
        print(f"[Notifier] TASK {task.id} retry #{attempt}: {error}")

    def _listen_loop(self, tasks_db) -> None:
        """
        审批监听循环。
        检测 tasks.json 变化，识别 GRaffe 的审批动作。
        """
        last_known: dict[str, str] = {}  # task_id → last known state

        # 初始化：记录所有 Pending Review 的状态
        all_tasks = tasks_db.load_all_tasks()
        for t in all_tasks:
            if t.state == "Pending Review":
                last_known[t.id] = t.state

        while self._running:
            time.sleep(10)  # 每 10s 检查一次

            try:
                all_tasks = tasks_db.load_all_tasks()
                current_pending = {t.id: t for t in all_tasks if t.state == "Pending Review"}
                current_done = {t.id: t for t in all_tasks if t.state == "Done"}
                current_todo = {t.id: t for t in all_tasks if t.state == "Todo"}

                # 检测：之前 Pending Review，现在变成 Done → 验收通过
                for task_id in list(last_known.keys()):
                    if task_id in current_done:
                        task = current_done[task_id]
                        print(f"[Notifier] GRaffe approved: {task_id}")
                        for event, cb in self._listeners:
                            if event == "approved":
                                try:
                                    cb(task)
                                except Exception as e:
                                    print(f"[Notifier] approved callback error: {e}")
                        del last_known[task_id]

                    # 检测：之前 Pending Review，现在变成 Todo → 打回重做
                    elif task_id in current_todo:
                        task = current_todo[task_id]
                        error_msg = task.error or ""
                        print(f"[Notifier] GRaffe requested revision: {task_id} — {error_msg}")
                        for event, cb in self._listeners:
                            if event == "revision":
                                try:
                                    cb(task, error_msg)
                                except Exception as e:
                                    print(f"[Notifier] revision callback error: {e}")
                        del last_known[task_id]

                # 补充新的 Pending Review 任务
                for task_id, task in current_pending.items():
                    if task_id not in last_known:
                        last_known[task_id] = task.state

            except Exception as e:
                print(f"[Notifier] listener error: {e}")
