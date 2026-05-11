#!/usr/bin/env python3
"""
opencode PTY wrapper for Symphony-Lite.
Detects permission prompts and automatically responds with "Allow always".
Handles the opencode TUI permission dialog: Allow once / Allow always / Reject
Sends SIGTERM after IDLE_TIMEOUT seconds of inactivity to force exit.

已知限制（2026-05-11）：
- PTY 使 opencode 检测到终端环境，渲染 TUI（ANSI 转义码）而非 headless 执行。
- 对于文档生成/报告类任务，PTY 模式产生大量不可读的日志，且 agent 写完文件后不退出。
- 如需 headless 执行，可考虑：移除 PTY 直接 pipe stdout，或改用 --no-tui 参数（视 opencode 版本支持情况）。
- 权限自动响应功能仅在 PTY 模式下可用，权衡取舍。

关键设计变更（fix: result.json-not-dependent-on-process-exit）：
- 不再用 os.setsid() 创建新 session：opencode 留在同一进程组，killpg 能整组杀死，
  waitpid 不会因 session leader zombie 而永久挂起。
- opencode 退出时（break 后）立即写 result.json + DONE marker，不依赖 finally。
- finally 只做资源清理（close fd），不做 waitpid（WNOHANG 在循环中已处理）。
"""
import errno
import json as _json
import os
import pty
import select
import signal
import termios
import time

TASK_ID = os.environ.get("TASK_ID", "")
PROMPT_FILE = os.environ.get("PROMPT_FILE", "")
RESULT_FILE = os.environ.get("RESULT_FILE", "")
WORKSPACE = os.environ.get("WORKSPACE", "")
HEARTBEAT = os.environ.get("HEARTBEAT", "")

# ── 超时配置 ───────────────────────────────────────────────────
EXIT_CODE = 0           # 模块级，opencode 退出时被设置
IDLE_TIMEOUT = 600       # 10min 无输出则强制退出
FORCE_KILL_DELAY = 30    # SIGTERM 后 30s 不退出则 SIGKILL

PERMISSION_PATTERNS = [
    b"Allow once",
    b"Allow always",
    b"Reject",
    b"Permission required",
    b"Permission denied",
]


def set_raw(fd):
    """Set terminal to raw mode."""
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP |
                  termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
    attrs[1] &= ~termios.OPOST
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8
    attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHOCTL |
                  termios.ECHOKE | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def send_key(fd, key_seq):
    os.write(fd, key_seq)


def _write_result(exit_code_val: int, permission_confirmed: bool) -> None:
    """写 result.json 和 DONE marker（opencode 退出后立即调用，不依赖 finally）。"""
    heartbeat_seq = -1
    if HEARTBEAT and os.path.exists(HEARTBEAT):
        try:
            with open(HEARTBEAT) as f:
                content = f.read().strip()
            parts = content.split()
            if parts:
                heartbeat_seq = int(parts[0])
        except (ValueError, OSError):
            pass

    result = {
        "task_id": TASK_ID,
        "exit_code": exit_code_val,
        "workspace": WORKSPACE,
        "heartbeat_file": HEARTBEAT,
        "heartbeat_seq": heartbeat_seq,
        "permission_auto_approved": permission_confirmed,
    }

    if RESULT_FILE:
        with open(RESULT_FILE, "w") as f:
            _json.dump(result, f, indent=2)

    # DONE marker：通知 bash wrapper "result.json 已就绪，可以退出了"
    if WORKSPACE:
        done_marker = os.path.join(WORKSPACE, ".done")
        try:
            with open(done_marker, "w") as f:
                f.write(f"{exit_code_val}\n")
        except OSError:
            pass


def main():
    # Read prompt
    with open(PROMPT_FILE, 'r', encoding='utf-8') as f:
        prompt_text = f.read()

    # Write initial heartbeat
    if HEARTBEAT:
        with open(HEARTBEAT, "w") as f:
            f.write(f"0 {int(time.time() * 1e9)}\n")

    # Create PTY pair
    master_fd, slave_fd = pty.openpty()
    set_raw(slave_fd)

    pid = os.fork()

    if pid == 0:
        # Child：不再用 os.setsid()，opencode 留在同一进程组
        # 这样 killpg 能杀死它，waitpid 不会挂死在 zombie 上
        os.close(master_fd)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(slave_fd)
        os.execvp("opencode", ["opencode", "--agent", "--prompt", PROMPT_FILE])
    else:
        # Parent
        global EXIT_CODE
        os.close(slave_fd)
        os.set_blocking(master_fd, False)

        buf = b""
        permission_active = False
        permission_confirmed = False
        opencode_exited = False
        log_file = os.path.join(WORKSPACE, "opencode.log") if WORKSPACE else "/tmp/opencode.log"
        last_output_time = time.time()
        turns_count = 0

        try:
            with open(log_file, "wb") as logf:
                while True:
                    elapsed = time.time() - last_output_time

                    # ── 超时检测：无输出超过 IDLE_TIMEOUT → SIGTERM ──
                    if elapsed > IDLE_TIMEOUT:
                        print(f"[wrapper] Idle timeout ({IDLE_TIMEOUT}s), sending SIGTERM to pid {pid}", flush=True)
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass

                        # 等其退出（FORCE_KILL_DELAY 秒）
                        for _ in range(FORCE_KILL_DELAY):
                            wpid, status = os.waitpid(pid, os.WNOHANG)
                            if wpid != 0:
                                EXIT_CODE = os.WEXITSTATUS(status)
                                print(f"[wrapper] Process exited after SIGTERM with code {EXIT_CODE}", flush=True)
                                opencode_exited = True
                                _write_result(EXIT_CODE, permission_confirmed)
                                break
                            time.sleep(1)
                        else:
                            print(f"[wrapper] Force killing pid {pid}", flush=True)
                            try:
                                os.kill(pid, signal.SIGKILL)
                                # 不调用 waitpid(, 0) —— 改用 WNOHANG 避免挂起
                                wpid, status = os.waitpid(pid, os.WNOHANG)
                                EXIT_CODE = os.WEXITSTATUS(status) if wpid != 0 else -9
                                opencode_exited = True
                                _write_result(EXIT_CODE, permission_confirmed)
                            except ProcessLookupError:
                                EXIT_CODE = -9
                                opencode_exited = True
                                _write_result(EXIT_CODE, permission_confirmed)
                        break

                    r, _, _ = select.select([master_fd], [], [], 1.0)
                    if master_fd in r:
                        try:
                            data = os.read(master_fd, 4096)
                            if not data:
                                print("[wrapper] PTY EOF, opencode finished", flush=True)
                                break
                            last_output_time = time.time()
                            buf += data
                            logf.write(data)
                            logf.flush()

                            if b">" in data:
                                turns_count += 1
                                print(f"[wrapper] Turn {turns_count}, idle={elapsed:.0f}s", flush=True)

                            if b"Permission required" in data or (b"Allow" in data and b"Reject" in data):
                                if not permission_active:
                                    print("[wrapper] Permission prompt detected, selecting 'Allow always'", flush=True)
                                    permission_active = True
                                    send_key(master_fd, b"\x1b[B")
                                    time.sleep(0.1)
                                    send_key(master_fd, b"\r")
                                    permission_active = False
                                    permission_confirmed = True

                        except OSError as e:
                            if e.errno == errno.EIO:
                                print("[wrapper] PTY EIO, breaking", flush=True)
                                break
                            raise

                    # 检查子进程是否退出（用 WNOHANG，不阻塞）
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid != 0:
                        EXIT_CODE = os.WEXITSTATUS(status)
                        print(f"[wrapper] opencode exited with code {EXIT_CODE}", flush=True)
                        opencode_exited = True
                        _write_result(EXIT_CODE, permission_confirmed)
                        break

        except Exception as e:
            print(f"[wrapper] Error: {e}", flush=True)
        finally:
            # 不再调用 waitpid(, 0) —— 已在循环中用 WNOHANG 处理过
            # opencode_exited=True 时 zombie 已被 WNOHANG 回收（waitpid 非 0 时自动回收）
            # opencode_exited=False 时（异常/崩溃）zombie 由 init 回收，无需我们处理
            try:
                os.close(master_fd)
            except OSError:
                pass

        # 如果异常退出（opencode_exited=False）没写到 result.json，补写兜底
        if not opencode_exited and RESULT_FILE and not os.path.exists(RESULT_FILE):
            _write_result(EXIT_CODE, permission_confirmed)

if __name__ == "__main__":
    main()
