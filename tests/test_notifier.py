"""
Test suite for FRAMEWORK/notifier.py

Covers:
- Notifier 初始化
- _notify 通知发送（HTTP POST → fallback 写文件到 inbox）
- notify_task_pending_review
- notify_task_retrying
- 审批监听 startup/stop
"""

import json
import os

from FRAMEWORK.config_loader import Config
from FRAMEWORK.notifier import Notifier
from FRAMEWORK.tasks_db import Task

# ── 辅助函数 ────────────────────────────────────────────────────


def make_config(inbox_dir: str = None, sillack_url: str = None) -> Config:
    cfg = Config._from_dict({
        "notification": {
            "sillack_web_url": sillack_url,
            "inbox_dir": inbox_dir or "/tmp/inbox",
            "enabled": True,
        },
    })
    # Notifier 读取 config 顶层属性，而非 config.notification
    # 所以需要显式设置
    if inbox_dir:
        cfg.inbox_dir = inbox_dir
    if sillack_url:
        cfg.sillack_web_url = sillack_url
    return cfg


def make_task(task_id: str = "T-NOTIF-001", state: str = "Done") -> Task:
    return Task(
        id=task_id, title=f"Task {task_id}", description="", state=state,
        priority=1, topic="test",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        acquired_at=None, acquired_by=None, lock_pid=None,
        attempt_count=1, error=None, result={}, metadata={},
    )


class TestNotifierInit:
    def test_default_config(self, temp_dir):
        """Notifier 使用默认配置，sillack_url 从 config 读取"""
        notifier = Notifier(make_config(inbox_dir=str(temp_dir)))
        assert os.path.exists(notifier.inbox_dir)

    def test_sillack_url_from_config(self, temp_dir):
        """sillack_web_url 来自 config 顶层的 sillack_web_url 属性"""
        cfg = make_config(inbox_dir=str(temp_dir))
        cfg.sillack_web_url = "https://example.com"
        notifier = Notifier(cfg)
        assert notifier.sillack_web_url == "https://example.com"

    def test_inbox_dir_created_on_init(self, temp_dir):
        """inbox 目录在 Notifier 初始化时创建"""
        inbox = str(temp_dir / "my_inbox")
        assert not os.path.exists(inbox)
        Notifier(make_config(inbox_dir=inbox))
        assert os.path.exists(inbox)


class TestNotifierNotify:
    def test_notify_task_pending_review_writes_file(self, temp_dir):
        """notify_task_pending_review 写入 inbox 文件"""
        inbox = str(temp_dir / "inbox")
        notifier = Notifier(make_config(inbox_dir=inbox, sillack_url="http://localhost:1"))
        task = make_task("T-REVIEW", state="Pending Review")
        notifier.notify_task_pending_review(task)

        # 检查 inbox 目录有文件
        files = os.listdir(inbox)
        assert len(files) >= 1
        # 读取内容验证
        found = False
        for fname in files:
            with open(os.path.join(inbox, fname)) as f:
                data = json.load(f)
            if data.get("task_id") == "T-REVIEW":
                assert data["event"] == "pending_review"
                found = True
                break
        assert found

    def test_notify_task_retrying_writes_file(self, temp_dir):
        """notify_task_retrying 写入 inbox 文件"""
        inbox = str(temp_dir / "inbox")
        notifier = Notifier(make_config(inbox_dir=inbox, sillack_url="http://localhost:1"))
        task = make_task("T-RETRY")
        notifier.notify_task_retrying(task, attempt=2, error="timeout")

        files = os.listdir(inbox)
        assert len(files) >= 1
        found = False
        for fname in files:
            with open(os.path.join(inbox, fname)) as f:
                data = json.load(f)
            if data.get("task_id") == "T-RETRY":
                assert data["event"] == "retrying"
                found = True
                break
        assert found


class TestNotifierListeners:
    def test_on_approved_registers_callback(self, temp_dir):
        """on_approved 注册回调"""
        notifier = Notifier(make_config(inbox_dir=str(temp_dir)))
        results = []

        def callback(task):
            results.append(task.id)

        notifier.on_approved(callback)
        assert len(notifier._listeners) == 1
        assert notifier._listeners[0][0] == "approved"

    def test_on_revision_registers_callback(self, temp_dir):
        """on_revision 注册回调"""
        notifier = Notifier(make_config(inbox_dir=str(temp_dir)))
        notifier.on_revision(lambda task, err: None)
        assert len(notifier._listeners) == 1
        assert notifier._listeners[0][0] == "revision"

    def test_start_stop_listening(self, temp_dir):
        """start/stop 监听线程"""
        notifier = Notifier(make_config(inbox_dir=str(temp_dir)))
        # Mock 一个 tasks_db 防止 None 错误
        class MockDB:
            def load_all_tasks(self):
                return []
        notifier.start_listening(MockDB())
        assert notifier._running is True
        assert notifier._listener_thread is not None
        assert notifier._listener_thread.is_alive()
        notifier.stop_listening()
        assert notifier._running is False
