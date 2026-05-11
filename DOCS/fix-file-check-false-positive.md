# file-check 误判修复实施记录

## 修复时间
2026-05-11

## 修复文件
- `FRAMEWORK/symphony_core.py`

## 触发的现场 Bug

### Bug 1：`.dispatch.lock` 不在 `_OUTPUT_SYSTEM_FILES` 排除列表

**症状**：Agent 退出码非零（exit 2 / SIGTERM）但任务被标记为成功。原因：
- Workspace 中 `.dispatch.lock`（4 字节 PID 锁文件）不被 `_OUTPUT_SYSTEM_FILES` 排除
- file-check 遍历非排除文件时，`.dispatch.lock` 通过了 `> 0` 字节检查
- `workspace_has_output = True` → 任务被误判为有产出

**修复**：将 `".dispatch.lock"` 加入 `_OUTPUT_SYSTEM_FILES` 集合。

### Bug 2：`os.path.getsize(fpath) > 0` 阈值过松

**症状**：任何非系统文件只要 > 0 字节就认为有实质性产出，4 字节的锁文件也通过。

**修复**：阈值从 `> 0` 提升到 `> 100` 字节。100 字节以下的文件不视为有意义的任务产出。

## 关联优化（可选）

`EXAMPLES/opencode_centric/` 下的 `opencode_pty_wrapper.py` 和 `agent_adapter.py` 使用了 PTY 封装 opencode，这会导致：
- opencode 检测到 PTY 后渲染 TUI（ANSI 转义码）
- 产生大量不可读的日志，且 agent 写完文件后不退出
- 此问题仅在 opencode TUI 交互模式中暴露，框架核心不受影响

## 验证方式

1. 创建一个写文件的测试任务
2. Agent 快速退出但文件已写入
3. Orchestrator 日志应出现 `task_success_via_file_check` + 状态置为 `Pending Review`
4. 如果 `.dispatch.lock` 是 workspace 中唯一的非系统文件，不应触发 file-check（正确进入 retry）
