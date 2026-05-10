"""
Test suite for FRAMEWORK/memory_manager.py

Covers:
- MemoryManager 初始化（config 参数）
- on_task_complete — 有 topic / 无 topic
- _make_slug — 标题转文件名
- _write_record — 记忆文件生成
- _append_index_entry — 追加索引
- _trim_index — 滑动窗口裁剪
"""

import os

import pytest

from FRAMEWORK.config_loader import Config
from FRAMEWORK.memory_manager import TOPIC_LABEL_MAP, MemoryManager
from FRAMEWORK.tasks_db import Task

# ── 辅助函数 ────────────────────────────────────────────────────


def make_config(index_root: str = None, max_entries: int = None,
                 min_entries: int = None) -> Config:
    cfg_dict = {
        "memory": {
            "constitution_file": "/tmp/constitution.md",
            "index_root": index_root or "/tmp/memory_index",
            "record_template": "{index_root}/{topic}/{date}-{slug}.md",
            "index_filename": ".index.md",
        },
    }
    if max_entries is not None:
        cfg_dict["memory"]["index_max_entries"] = max_entries
    if min_entries is not None:
        cfg_dict["memory"]["index_min_entries"] = min_entries
    return Config._from_dict(cfg_dict)


def make_task(task_id: str = "T-MEM-001", topic: str = "test", title: str = "Test task") -> Task:
    return Task(
        id=task_id, title=title, description="", state="Done",
        priority=1, topic=topic,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        acquired_at=None, acquired_by=None, lock_pid=None,
        attempt_count=1, error=None, result={"key": "val"}, metadata={},
    )


# ── 初始化 ──────────────────────────────────────────────────────


class TestMemoryManagerInit:
    def test_default_config(self, temp_dir):
        """MemoryManager 从 config 正确初始化"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        assert mgr.index_root == str(temp_dir)
        assert mgr.index_filename == ".index.md"
        assert mgr.index_max_entries == 50
        assert mgr.keyword_filter is True


# ── _make_slug ──────────────────────────────────────────────────


class TestMakeSlug:
    def test_basic_slug(self):
        mgr = MemoryManager(make_config())
        assert mgr._make_slug("Hello World") == "Hello-World"

    def test_chinese_chars(self):
        mgr = MemoryManager(make_config())
        slug = mgr._make_slug("测试任务")
        assert slug

    def test_special_chars_to_dash(self):
        mgr = MemoryManager(make_config())
        slug = mgr._make_slug("a/b/c:d")
        assert "---" not in slug  # 连续横线已合并
        assert slug == "a-b-c-d"

    def test_long_title_truncated(self):
        mgr = MemoryManager(make_config())
        long_title = "x" * 100
        slug = mgr._make_slug(long_title)
        assert len(slug) <= 50

    def test_empty_title_fallback(self):
        mgr = MemoryManager(make_config())
        assert mgr._make_slug("") == "untitled"


# ── on_task_complete ───────────────────────────────────────────


class TestOnTaskComplete:
    def test_no_topic_skips(self, temp_dir):
        """无 topic 的任务跳过记忆生成"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        task = make_task(topic=None)
        # 不应该报错
        mgr.on_task_complete(task)
        # 目录不应被创建
        assert not os.path.exists(mgr._index_path("test"))

    def test_with_topic_creates_record(self, temp_dir):
        """有 topic 的任务创建记录文件"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        task = make_task(topic="test")
        mgr.on_task_complete(task, stdout="completed successfully")

        # 检查记录文件
        topic_dir = os.path.join(str(temp_dir), "test")
        assert os.path.exists(topic_dir)
        # 应该有 .index.md
        index_path = os.path.join(topic_dir, ".index.md")
        assert os.path.exists(index_path)

    def test_record_file_content(self, temp_dir):
        """记录文件包含任务信息"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        task = make_task(topic="test", title="My Test Task")
        mgr.on_task_complete(task, stdout="done")

        # 找记录文件
        topic_dir = os.path.join(str(temp_dir), "test")
        files = [f for f in os.listdir(topic_dir) if f != ".index.md"]
        assert len(files) >= 1
        record_path = os.path.join(topic_dir, files[0])
        content = open(record_path).read()
        assert "T-MEM-001" in content
        assert "My Test Task" in content

    def test_index_file_format(self, temp_dir):
        """.index.md 包含正确的 markdown table 格式"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        mgr.on_task_complete(make_task(topic="test"), stdout="done")

        index_path = os.path.join(str(temp_dir), "test", ".index.md")
        content = open(index_path).read()
        assert "| 日期 |" in content
        assert "T-MEM-001" in content or "test" in content.lower()


# ── _trim_index ────────────────────────────────────────────────


class TestTrimIndex:
    def test_trims_over_max_entries(self, temp_dir):
        """超过 max_entries 时裁剪"""
        cfg = make_config(index_root=str(temp_dir))
        # MemoryConfig 没有 index_max_entries/index_min_entries 字段
        # 需要直接设到对象上供 MemoryManager.getattr 读取
        cfg.memory.index_max_entries = 3
        cfg.memory.index_min_entries = 1
        mgr = MemoryManager(cfg)
        for i in range(5):
            t = make_task(task_id=f"T-{i:03d}", topic="test", title=f"Task {i}")
            mgr.on_task_complete(t, stdout=f"result {i}")

        index_path = os.path.join(str(temp_dir), "test", ".index.md")
        content = open(index_path).read()
        # 数据行（不含 header）
        data_rows = [x for x in content.split("\n") if x.startswith("|") and "---" not in x
                     and "日期" not in x]
        assert len(data_rows) <= 3  # max 3 data rows
        # 最新的任务在列表中
        assert "Task-4" in content

    def test_no_trim_below_max(self, temp_dir):
        """不超过 max_entries 时不裁剪"""
        cfg = make_config(index_root=str(temp_dir), max_entries=10)
        mgr = MemoryManager(cfg)
        for i in range(3):
            mgr.on_task_complete(make_task(task_id=f"T-{i}", topic="test"), stdout="ok")

        index_path = os.path.join(str(temp_dir), "test", ".index.md")
        content = open(index_path).read()
        data_rows = [x for x in content.split("\n") if x.startswith("|") and "---" not in x
                     and "日期" not in x]
        assert len(data_rows) == 3  # 3 data rows


# ── _write_record ──────────────────────────────────────────────


class TestWriteRecord:
    def test_writes_structured_fields(self, temp_dir):
        """记录文件包含 structured fields 区块"""
        cfg = make_config(index_root=str(temp_dir))
        mgr = MemoryManager(cfg)
        task = make_task(topic="test", title="Discovery task")
        mgr._write_record(
            path=str(temp_dir / "record.md"),
            task=task,
            stdout="",
            stderr="",
            exit_code=0,
            discoveries=["Found bug A", "Found bug B"],
            decisions=["Fix with refactor"],
            risks=["Risk of regression"],
        )
        content = open(str(temp_dir / "record.md")).read()
        assert "发现（Discoveries）" in content
        assert "Found bug A" in content
        assert "决策（Decisions）" in content
        assert "Fix with refactor" in content
        assert "风险与坑点（Risks）" in content
        assert "Risk of regression" in content


# ── TOPIC_LABEL_MAP ──────────────────────────────────────────


class TestTopicLabelMap:
    def test_known_topics(self):
        assert TOPIC_LABEL_MAP["iran"] == "伊朗新闻与地缘"
        assert TOPIC_LABEL_MAP["daily-report"] == "日报"
        assert TOPIC_LABEL_MAP["project"] == "项目设计与决策"

    def test_unknown_topic(self):
        with pytest.raises(KeyError):
            _ = TOPIC_LABEL_MAP["unknown"]
