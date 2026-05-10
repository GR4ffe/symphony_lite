"""
Symphony-Lite OpenCode 适配器
EXAMPLES/opencode_centric/agent_adapter.py

职责：
- 启动 OpenCode 包装脚本子进程（方案 A：wrapper + 心跳）
- 管理 session lifecycle
- 等待进程结束，读取 result.json
- 提供 wait_session() 阻塞调用给 Orchestrator
"""
import errno
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import Config
    from workspace_mgr import Workspace


@dataclass
class Session:
    """Agent 执行会话"""
    id: str            # "{pid}-1" 格式
    pid: int           # 操作系统 PID
    workspace: "Workspace | None" = None
    process: "subprocess.Popen | None" = None


@dataclass
class AgentResult:
    """Agent 执行结果"""
    status: str        # "success" | "failed"
    exit_code: int
    stdout: str
    stderr: str
    result: dict        # 从 result.json 解析


class OpenCodeAgent:
    """
    OpenCode 执行器。

    关键设计：
    - 每个 Session 持自己的 process 引用（并发安全）
    - start_session() 非阻塞，返回 Session 对象
    - wait_session() 阻塞直到 agent 结束（由 worker 线程调用）
    """

    def __init__(self, config: "Config"):
        self.config = config
        self.opencode_cmd = config.agent.opencode.command
        self.max_turns = config.agent.opencode.max_turns

    def start_session(self, workspace: "Workspace", prompt: str) -> Session:
        """
        1. 把 prompt 写入 workspace/prompt.txt
        2. 生成并写入 wrapper 脚本
        3. 启动 wrapper 子进程
        4. 返回 Session 对象（持 process 引用）
        """
        prompt_file = os.path.join(workspace.path, "prompt.txt")
        result_file = os.path.join(workspace.path, "result.json")

        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)

        wrapper_script = self._build_wrapper_script(workspace.task_id)
        wrapper_file = os.path.join(workspace.path, "opencode_wrapper.sh")
        with open(wrapper_file, "w", encoding="utf-8") as f:
            f.write(wrapper_script)
        os.chmod(wrapper_file, 0o755)

        proc = subprocess.Popen(
            ["bash", wrapper_file, workspace.task_id, prompt_file, result_file],
            cwd=workspace.path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # I3：创建新 session，使 proc.pid 成为进程组 PGID，配合 killpg 可整树杀死
            preexec_fn=os.setsid,
        )

        session_id = f"{proc.pid}-1"
        return Session(
            id=session_id,
            pid=proc.pid,
            workspace=workspace,
            process=proc,
        )

    def _build_wrapper_script(self, task_id: str) -> str:
        """
        生成 opencode_wrapper.sh（Python PTY 封装，自动响应 opencode 权限提示）。

        职责分工：
        - Python 进程：启动 bash 子进程运行 opencode（PTY 模式），检测权限提示并自动点"Allow always"
        - bash 心跳：后台守护进程，每 30s 更新 .heartbeat mtime

        权限自动应答逻辑：
        - opencode 的 TUI 权限对话框：Allow once / Allow always / Reject
        - 检测到 "Permission required" 或 "Allow" 关键词时，发送 ↓+Enter 选择 "Allow always"
        """
        # 解析 pty_wrapper_script 为绝对路径（相对于 agent_adapter.py 所在目录）
        import pathlib
        import textwrap
        adapter_dir = pathlib.Path(__file__).resolve().parent
        pty_wrapper = str((adapter_dir / self.config.agent.opencode.pty_wrapper_script).resolve())
        return textwrap.dedent(f'''\
            #!/bin/bash
            set -euo pipefail

            TASK_ID="{task_id}"
            PROMPT_FILE="$2"
            RESULT_FILE="$3"
            WORKSPACE="$(pwd)"
            HEARTBEAT="${{WORKSPACE}}/.heartbeat"
            DONE_MARKER="${{WORKSPACE}}/.done"

            # ── 心跳循环（后台守护，写入单调递增序列号）─────────────
            update_heartbeat() {{
                local seq=0
                while true; do
                    if ! kill -0 $$ 2>/dev/null; then exit 0; fi
                    # 写入序列号 + 纳秒时间戳（免疫时钟回拨）
                    echo "$seq $(date +%s%N)" > "$HEARTBEAT"
                    seq=$((seq + 1))
                    sleep 30
                done
            }}
            update_heartbeat &
            HEARTBEAT_PID=$!

            # ── Python PTY wrapper（处理权限提示）──────────────────
            # 关键变更：不等 Python 进程退出，而是等 DONE marker 出现。
            # 这样即如果 Python 被 kill 掉（没执行到写 result.json），
            # 兜底逻辑会补写；如果 Python 正常写完，bash 立即退出，不挂起。
            export TASK_ID PROMPT_FILE RESULT_FILE WORKSPACE HEARTBEAT
            python3 {pty_wrapper} &
            PYTHON_PID=$!

            # 等待 DONE marker 出现（最多 3600s = 1h），超时则强制终止 Python
            _deadline=$(( $(date +%s) + 3600 ))
            while [ ! -f "$DONE_MARKER" ]; do
                # Python 已退出但 marker 没出现：补写 result.json 并退出
                if ! kill -0 $PYTHON_PID 2>/dev/null; then
                    echo "[wrapper] Python exited without DONE marker, writing fallback result.json" >&2
                    break
                fi
                if [ $(date +%s) -ge $_deadline ]; then
                    echo "[wrapper] DONE marker timeout, killing Python" >&2
                    kill -KILL --(-$$) $PYTHON_PID 2>/dev/null || kill -KILL $PYTHON_PID 2>/dev/null || true
                    wait $PYTHON_PID 2>/dev/null || true
                    break
                fi
                sleep 2
            done

            # 清理心跳守护
            kill $HEARTBEAT_PID 2>/dev/null || true
            wait $HEARTBEAT_PID 2>/dev/null || true

            # ── 关键修复：.done 出现后，显式杀死 python PTY wrapper 及 opencode ──
            # 否则 python 变成孤儿后卡在 select()，opencode 永远不退出
            if kill -0 $PYTHON_PID 2>/dev/null; then
                echo "[wrapper] DONE detected, killing PTY wrapper (pid $PYTHON_PID)" >&2
                # 先 TERM 优雅退出（给 python 机会 close(masterFd) → PTY EOF → opencode 退出）
                kill -TERM $PYTHON_PID 2>/dev/null || true
                # 等 5s 优雅退出，超时则 KILL
                for _ in $(seq 1 5); do
                    if ! kill -0 $PYTHON_PID 2>/dev/null; then
                        echo "[wrapper] PTY wrapper exited gracefully" >&2
                        break
                    fi
                    sleep 1
                done
                # 仍活着？SIGKILL 整组
                if kill -0 $PYTHON_PID 2>/dev/null; then
                    echo "[wrapper] Force killing PTY wrapper and process group" >&2
                    kill -KILL --(-$$) $PYTHON_PID 2>/dev/null || kill -KILL $PYTHON_PID 2>/dev/null || true
                fi
                wait $PYTHON_PID 2>/dev/null || true
            fi

            # 补写序列号
            _raw=$(cat "$HEARTBEAT" 2>/dev/null)
            if [ -n "$_raw" ]; then
                last_seq=$(echo "$_raw" | cut -d' ' -f1)
                case "$last_seq" in
                    ''|*[!0-9]*) last_seq=-1 ;;
                esac
            else
                last_seq=-1
            fi
            next_seq=$((last_seq + 1))
            echo "$next_seq $(date +%s%N)" > "$HEARTBEAT"

            # ── 兜底：如果 result.json 不存在（Python 没写完就被杀了）─────────
            if [ ! -f "$RESULT_FILE" ]; then
                _seq=$(cat "$HEARTBEAT" 2>/dev/null | cut -d' ' -f1)
                [ -z "$_seq" ] && _seq=-1
                _exit_code=143  # SIGTERM
                [ -f "$DONE_MARKER" ] && _exit_code=$(cat "$DONE_MARKER" 2>/dev/null | head -1 || echo 143)
                cat > "$RESULT_FILE" <<EOF
            {{
              "task_id": "$TASK_ID",
              "exit_code": $_exit_code,
              "workspace": "$WORKSPACE",
              "heartbeat_file": "$HEARTBEAT",
              "heartbeat_seq": $_seq
            }}
            EOF
            fi

            exit 0
        ''')

    def _read_log_tail(self, workspace_path: str, max_bytes: int = 4096) -> str:
        """读取 opencode.log 尾部内容（用于捕获 agent 真实输出/错误）"""
        log_path = os.path.join(workspace_path, "opencode.log")
        if not os.path.isfile(log_path):
            return ""
        try:
            size = os.path.getsize(log_path)
            if size == 0:
                return ""
            with open(log_path, "rb") as f:
                # 只读最后 max_bytes（跳过开头避免 OOM）
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                    # 跳到最近换行避免截断行
                    leftover = f.read(max_bytes)
                    nl_pos = leftover.find(b"\n")
                    if nl_pos >= 0:
                        f.seek(-max_bytes + nl_pos + 1, os.SEEK_END)
                    else:
                        f.seek(-max_bytes, os.SEEK_END)
                data = f.read()
            # 过滤 ANSI 控制字符
            text = data.decode("utf-8", errors="replace")
            import re
            text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            return text.strip()
        except Exception:
            return ""

    def wait_session(self, session: Session) -> AgentResult:
        """
        非阻塞等待 OpenCode 完成：每 2s poll proc 状态 + 检查 result.json 是否就绪。
        即便 bash wrapper / Python PTY wrapper 卡住不退出，只要 result.json 出现就立即返回。
        兜底超时：3600s（与 bash wrapper DONE marker 等待一致）。

        FIX (Bug 2): 所有 return 路径读取 opencode.log 尾部注入 stdout，
        使 _on_agent_done 能看到 agent 真实输出，而不是永远空字符串。
        """
        proc = session.process
        result_file = os.path.join(session.workspace.path, "result.json")
        deadline = time.time() + 3600  # 1h 超时兜底

        def _build_result(status, exit_code, stderr, result_data):
            log_tail = self._read_log_tail(session.workspace.path)
            return AgentResult(
                status=status,
                exit_code=exit_code,
                stdout=log_tail,
                stderr=stderr,
                result=result_data,
            )

        while time.time() < deadline:
            # result.json 一出现就立即读并返回（不等 wrapper 进程退出）
            if os.path.exists(result_file):
                try:
                    with open(result_file) as f:
                        result_data = json.load(f)
                    # proc.poll() 非阻塞，立即返回 None 或 exit code
                    poll_result = proc.poll()
                    exit_code = poll_result if poll_result is not None else 0
                    return _build_result(
                        status="success" if exit_code == 0 else "failed",
                        exit_code=exit_code,
                        stderr="",
                        result_data=result_data,
                    )
                except (json.JSONDecodeError, OSError):
                    pass

            # proc 已退出但 result.json 还没被 bash wrapper 写完（罕见）
            poll_result = proc.poll()
            if poll_result is not None:
                # 进程已退出，再等最多 10s 让 bash wrapper 收尾并写 result.json
                for _ in range(20):  # 10s * 2 = 20 次 * 0.5s
                    time.sleep(0.5)
                    if os.path.exists(result_file):
                        try:
                            with open(result_file) as f:
                                result_data = json.load(f)
                            return _build_result(
                                status="success" if poll_result == 0 else "failed",
                                exit_code=poll_result,
                                stderr="",
                                result_data=result_data,
                            )
                        except (json.JSONDecodeError, OSError):
                            pass
                # 超时：进程退出但 result.json 仍然不存在，返回兜底
                return _build_result(
                    status="failed",
                    exit_code=poll_result,
                    stderr="result.json not found after proc exit (timeout waiting for DONE marker)",
                    result_data={},
                )

            time.sleep(2.0)

        # 超时兜底：强制杀进程，读已存在的 result.json（如果有）
        proc.kill()
        proc.wait()
        if os.path.exists(result_file):
            try:
                with open(result_file) as f:
                    result_data = json.load(f)
                return _build_result(
                    status="failed",
                    exit_code=-1,
                    stderr="wait_session timeout, proc killed",
                    result_data=result_data,
                )
            except (json.JSONDecodeError, OSError):
                pass
        return _build_result(
            status="failed",
            exit_code=-1,
            stderr="wait_session timeout, no result.json found",
            result_data={},
        )

    def stop_session(self, session: Session) -> None:
        """
        强制终止子进程及其整个进程树（用 killpg 确保连根拔起）。
        I3：解决 wrapper.sh 被 kill 时 opencode 子进程变孤儿的问题。
        """
        if session is None:
            return
        # 支持 RecoveredSession（无 process，只有 pid）
        pid = session.process.pid if session.process else getattr(session, 'pid', None)
        if not pid:
            return
        try:
            pgid = os.getpgid(pid)
            # 先 SIGTERM，给进程优雅退出的机会
            os.killpg(pgid, signal.SIGTERM)
            # 等待进程退出（最多 10s）
            try:
                if session.process:
                    session.process.wait(timeout=10)
                else:
                    # RecoveredSession：直接 wait pid
                    os.waitpid(pid, 0)
            except subprocess.TimeoutExpired:
                pass
        except ProcessLookupError:
            # 进程组已经不存在了，说明已经退出
            pass
        except OSError as e:
            if e.errno not in (errno.ESRCH, errno.ECHILD):
                raise
        finally:
            session.process = None

    def is_process_alive(self, pid: int) -> bool:
        """检查 PID 是否还在运行（跨平台）"""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
