"""
Symphony-Lite 文件监视器
FRAMEWORK/file_watcher.py

使用 inotify 监控 tasks.json 变化（Phase 4 增强）。
当 inotify 不可用时（WSL1/Windows），自动降级为轮询。
"""
import os
import threading
import time
from typing import Callable


class FileWatcher:
    """
    文件监视器。

    支持：
    - inotify（Linux，生产级）
    - 轮询降级（WSL1/Windows，每秒检查一次）

    使用方式：
        watcher = FileWatcher("/path/to/tasks.json")
        watcher.on_change(lambda path: print(f"{path} changed!"))
        watcher.start()
        # 结束时：
        watcher.stop()
    """

    def __init__(self, file_path: str, poll_interval: float = 1.0):
        self.file_path = os.path.abspath(file_path)
        self.poll_interval = poll_interval
        self._callbacks: list[Callable[[str], None]] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_mtime: float | None = None
        self._last_size: int | None = None

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # 初始化
        if os.path.exists(self.file_path):
            stat = os.stat(self.file_path)
            self._last_mtime = stat.st_mtime
            self._last_size = stat.st_size
        self._thread = threading.Thread(target=self._run, name="file-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        if self._use_inotify():
            self._run_inotify()
        else:
            self._run_poll()

    def _use_inotify(self) -> bool:
        """检测 inotify 是否可用（Linux with inotify_init）"""
        try:
            import ctypes, ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            return hasattr(libc, "inotify_init")
        except Exception:
            return False

    def _run_inotify(self) -> None:
        """inotify 监控（生产级，事件驱动）"""
        try:
            import inotify.adapters
            i = inotify.adapters.Inotify()
            i.add_watch(self.file_path)
            print(f"[FileWatcher] Using inotify for {self.file_path}")
            for event in i.event_gen():
                if not self._running:
                    break
                if event is None:
                    continue
                _, type_names, path, filename = event
                if "IN_MODIFY" in type_names or "IN_CLOSE_WRITE" in type_names:
                    self._notify()
        except Exception as e:
            print(f"[FileWatcher] inotify failed ({e}), falling back to poll")
            self._run_poll()

    def _run_poll(self) -> None:
        """轮询监控（降级方案）"""
        print(f"[FileWatcher] Using poll for {self.file_path} (every {self.poll_interval}s)")
        while self._running:
            try:
                if os.path.exists(self.file_path):
                    stat = os.stat(self.file_path)
                    changed = (
                        self._last_mtime is None
                        or stat.st_mtime != self._last_mtime
                        or stat.st_size != self._last_size
                    )
                    if changed:
                        self._last_mtime = stat.st_mtime
                        self._last_size = stat.st_size
                        self._notify()
            except Exception:
                pass
            time.sleep(self.poll_interval)

    def _notify(self) -> None:
        for cb in self._callbacks:
            try:
                cb(self.file_path)
            except Exception as e:
                print(f"[FileWatcher] callback error: {e}")
