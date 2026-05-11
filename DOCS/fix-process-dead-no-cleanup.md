# `process_dead` 不清理 Workspace 实施文档

## 背景

### 问题描述

当 Agent 进程（opencode）因 `timeout` 正常退出时，`_terminate(cleanup=True)` 无条件删除 workspace 目录，与 `_on_agent_done()` 的 file-check 产生竞态：

```
Worker 线程 wait_session() → result.json 就绪 → _on_agent_done()
                                 VS
Tick 线程 _reconcile_running() → process_dead → _terminate(cleanup=True) → 删 workspace
```

Tick 线程若赢得竞态，workspace 被清空，`_on_agent_done()` 找不到产出文件，即便 agent 已写了完整文档，任务仍被判定失败并重试。

### 根因

`process_dead` 路径的 `cleanup=True` 和正常完成路径的 file-check 之间，缺少一个**协调手段**。`process_dead` 做出了"进程死了肯定没产出"的假设，这个假设在以下场景不成立：

- TUI agent（如 opencode）：写文件快，退出慢。`timeout` 到期后进程才死，但文件早已写完
- Worker 线程异步处理结果：`wait_session()` 独立于 tick 循环运行，天然有竞态窗口

## 方案：`process_dead` 不清理 Workspace

### 设计原则：职责分离

```
改前：进程死 → 一定没产出 → 删 workspace
改后：进程死 → 不确定有没有产出 → 留 workspace 让 file-check 判断
```

### 具体改动

**文件**：`FRAMEWORK/symphony_core.py`（运行版：`/win-hermes/symphony/symphony_core.py`）

**改动位置**：`_reconcile_running()` 方法中的 `process_dead` 检测分支（约第 808 行附近）

```python
# 改前：
self._terminate(task_id, cleanup=True)

# 改后：
self._terminate(task_id, cleanup=False)
```

### 受影响模块

| 模块 | 影响 | 说明 |
|------|------|------|
| `symphony_core.py:_reconcile_running()` | 直接修改 | `process_dead` 分支 cleanup 参数 |
| `workspace_mgr.py:prepare()` | 无影响 | 已处理 workspace 已存在 + stale lock 清理 |
| `workspace_mgr.py:cleanup()` | 调用减少 | 不再被 `process_dead` 调用，仅由 GC 和正常完成路径触发 |
| `agent_adapter.py` | 无影响 | `stop_session()` 杀进程树逻辑不变 |

### 技术推演

#### 场景 1：Agent 写了文件后被 timeout 杀（正常情况）

```
[第 10 分钟]
1. timeout 600 到期 → SIGTERM opencode
2. wrapper → 写 result.json → exit 0
3. Tick: process_dead 检测到 PID 不在了 → _terminate(cleanup=False) → 不删 workspace
4. Worker: wait_session() 读到 result.json → _on_agent_done()
   → 查 workspace：agent_long_term_memory_survey.md（14KB > 100）
   → workspace_has_output = True → state = Pending Review
```

✅ 正确完成。

#### 场景 2：Agent 什么都没写就崩溃了

```
1. Agent 崩溃 → wrapper 写 result.json (exit_code != 0) → exit
2. Tick: process_dead → _terminate(cleanup=False)
3. Worker: wait_session() 读到 result.json → _on_agent_done()
   → 查 workspace：只有 .dispatch.lock(排除), .heartbeat(排除), opencode.log(排除)...
   → workspace_has_output = False → 进 retry
```

✅ 正确进入重试。

#### 场景 3：Worker 线程还没处理完，重试的 dispatch 先开始了

```
1. Tick: process_dead → _terminate(cleanup=False) → _schedule_retry()
2. 下一个 tick: 重试 dispatch → prepare() 复用 workspace
   → _clean_stale_lock() 清理旧的 .dispatch.lock（PID 已死）
   → 写新的 prompt.txt（覆盖旧的）
   → 启动新的 wrapper
3. 新的 opencode 启动，开始处理新的 prompt
4. （可能同时）旧的 worker 线程还在等 wait_session() 返回
```

⚠️ 这里有一个**残留风险**：旧的 worker 线程和新的 wrapper 同时写同一个 workspace。

**分析**：
- 旧 wrapper 的 result.json 已在旧 wrapper exit 时写入，不会被新 wrapper 覆盖（新 wrapper 也会写自己的 result.json）
- 旧 worker 的 `_on_agent_done()` 和新 wrapper 的 opencode 几乎不可能**同时访问同一个文件**
- 最坏情况：旧 worker 读到新 result.json → 但这不太可能，因为 session 对象指向旧的 process

**结论**：风险很低，且 `_terminate()` 中调用的 `stop_session()` 已经杀掉了旧进程组，新 dispatch 不可能和旧进程共存。

#### 场景 4：Agent 完成正常退出

```
1. Agent 写完文件后正常退出（退出码 0）
2. wrapper 写 result.json → exit 0
3. wait_session() 读到 result.json
4. _on_agent_done() 正常执行
5. _terminate(cleanup=False) → 不删 workspace（等用户验收或 GC 清理）
```

✅ 正常路径完全不受影响。

### 安全性分析

#### 磁盘占用

每个 retry 会累积旧文件。按最差情况估算：

| 参数 | 值 |
|------|-----|
| 每次重试产生文件 | ~20KB（文档 + log）|
| 最大重试次数 | 10 |
| 单任务最大占用 | ~200KB |
| 并发任务数 | 3（max_concurrent） |
| 总占用 | ~600KB |

可忽略。且 24h GC 会清理已完成的 workspace。

#### 数据残留

旧文件不会干扰新 opencode，因为：
- `prompt.txt` 被新 dispatch 覆盖
- opencode 不自动读取 workspace 已有文件
- 如果新 opencode 写同名文件，覆盖即可

### 测试验证

#### 验证场景 1：file-check 能兜底

1. 创建文档类任务
2. Agent 写文件后等待 timeout（不主动退出）
3. 确认日志出现 `task_success_via_file_check` 或 `task_success`
4. 确认文件在，状态为 `Pending Review`

#### 验证场景 2：崩溃无产出时正确重试

1. 创建无法完成的任务（如不存在的路径）
2. 确认进程退出后状态进入 `Todo` + 重试队列

#### 验证场景 3：正常完成不受影响

1. Agent 正常退出（exit 0 + 文件存在）
2. 确认状态直接 `Pending Review`

---

## 修复合集

实施前确认以下 4 个修复已全部到位（本次只需实施 #4）：

| # | 修复 | 文件 | 状态 |
|---|------|------|------|
| 1 | `.dispatch.lock` 加入 `_OUTPUT_SYSTEM_FILES` | `symphony_core.py` | ✅ 已提交到 repo |
| 2 | 文件阈值 `> 0` → `> 100` | `symphony_core.py` | ✅ 已提交到 repo |
| 3 | `open_code_pty_wrapper.py:--prompt` 传文件路径 | `opencode_pty_wrapper.py` | ✅ 已提交到 repo |
| 4 | `process_dead` 不 cleanup workspace | `symphony_core.py` | 🔲 本次实施 |
