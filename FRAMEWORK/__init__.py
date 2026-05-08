# Symphony-Lite Framework Core
#
# This package contains the agent-agnostic core of Symphony-Lite.
# It is designed to be independent of any specific agent runtime.
#
# Key modules:
#   symphony_core   — Tick loop, state machine, dispatch orchestration
#   tasks_db        — Task state persistence (tasks.json)
#   workspace_mgr   — Per-task workspace lifecycle
#   config_loader   — YAML configuration loading
#   memory_manager  — Cross-task memory system
#   notifier        — Approval/revision notifications
#   file_watcher    — tasks.json change monitoring
#   logger          — Structured rotating log
#   utils           — Shared utilities
