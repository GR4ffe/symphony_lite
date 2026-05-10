"""
Test suite for FRAMEWORK/file_watcher.py

Covers:
- FileWatcher 初始化
- start/stop 生命周期
- 文件变化触发回调
- 不触发首次加载的假阳性
"""

import threading
import time

from FRAMEWORK.file_watcher import FileWatcher


class TestFileWatcherInit:
    def test_construct(self, temp_dir):
        """FileWatcher 构造"""
        fw = FileWatcher(str(temp_dir / "test.json"))
        assert fw.file_path == str(temp_dir / "test.json")
        assert fw.poll_interval == 1.0
        assert fw._callbacks == []
        assert fw._running is False

    def test_custom_poll_interval(self, temp_dir):
        fw = FileWatcher(str(temp_dir / "test.json"), poll_interval=0.1)
        assert fw.poll_interval == 0.1


class TestFileWatcherLifecycle:
    def test_start_stop(self, temp_dir):
        """start/stop 不报错"""
        fw = FileWatcher(str(temp_dir / "test.json"))
        fw.start()
        assert fw._running is True
        assert fw._thread is not None
        assert fw._thread.is_alive()
        fw.stop()
        assert fw._running is False
        # 线程应已结束
        fw._thread.join(timeout=5)

    def test_double_start_stop(self, temp_dir):
        """重复 start/stop 安全"""
        fw = FileWatcher(str(temp_dir / "test.json"))
        fw.start()
        fw.start()  # 二次 start 不应报错
        fw.stop()
        fw.stop()  # 二次 stop 不应报错
        assert fw._running is False

    def test_callback_on_file_change(self, temp_dir):
        """文件变化触发回调"""
        test_file = str(temp_dir / "test.json")
        # 创建文件
        with open(test_file, "w") as f:
            f.write("{}")

        callback_called = threading.Event()
        callback_path = []

        def cb(path):
            callback_path.append(path)
            callback_called.set()

        fw = FileWatcher(test_file, poll_interval=0.1)
        fw.on_change(cb)
        fw.start()

        try:
            # 修改文件
            time.sleep(0.3)  # 让 watcher 完成初始化
            with open(test_file, "w") as f:
                f.write('{"key": "value"}')

            # 等待回调
            assert callback_called.wait(timeout=5), "Callback was not called"
            assert callback_path[0] == test_file
        finally:
            fw.stop()

    def test_no_false_positive_on_start(self, temp_dir):
        """启动时不触发回调"""
        test_file = str(temp_dir / "test.json")
        with open(test_file, "w") as f:
            f.write("{}")

        callback_called = []

        def cb(path):
            callback_called.append(path)

        fw = FileWatcher(test_file, poll_interval=0.1)
        fw.start()
        try:
            time.sleep(0.3)
            assert len(callback_called) == 0, "Callback triggered on start"
        finally:
            fw.stop()

    def test_nonexistent_file_no_error(self, temp_dir):
        """不存在的文件不报错"""
        fw = FileWatcher(str(temp_dir / "nonexistent.json"), poll_interval=0.1)
        fw.start()
        try:
            time.sleep(0.3)
            # 创建文件不应报错
            with open(str(temp_dir / "nonexistent.json"), "w") as f:
                f.write("data")
            time.sleep(0.3)
        finally:
            fw.stop()
