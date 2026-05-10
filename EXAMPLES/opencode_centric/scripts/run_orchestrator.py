#!/usr/bin/env python3
"""
Symphony-Lite 启动脚本
EXAMPLES/opencode_centric/scripts/run_orchestrator.py

用法：
  python3 scripts/run_orchestrator.py

依赖：
  - FRAMEWORK/ 所有模块（symphony_core, tasks_db, workspace_mgr, ...）
  - config.yaml 中的所有路径均为相对路径（相对于项目根目录）

环境变量（可选）：
  SILLACK_WEB_URL   — sillack-web 通知端点
  SYMPHONY_CONFIG   — config.yaml 路径（默认：configs/config.yaml）
"""
import os
import sys

# 把 FRAMEWORK 目录加入 import path
FRAMEWORK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "FRAMEWORK")
sys.path.insert(0, os.path.abspath(FRAMEWORK_DIR))

# 把 EXAMPLES/opencode_centric 也加入 path（agent_adapter 在这里）
EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(EXAMPLE_DIR))

from config_loader import load_config
from symphony_core import SymphonyOrchestrator


def main():
    config_path = os.environ.get(
        "SYMPHONY_CONFIG",
        os.path.join(os.path.dirname(__file__), "..", "configs", "config.yaml"),
    )
    config_path = os.path.abspath(config_path)

    print(f"[run_orchestrator] Loading config from: {config_path}")
    config = load_config(config_path)

    orchestrator = SymphonyOrchestrator(config)

    print("[run_orchestrator] Starting Symphony-Lite orchestrator...")
    print(f"[run_orchestrator] tasks_file: {config.tracker.tasks_file}")
    print(f"[run_orchestrator] workspace_root: {config.workspace.root}")
    print(f"[run_orchestrator] agent executor: {config.agent.executor}")
    print(f"[run_orchestrator] max_concurrent_agents: {config.agent.max_concurrent_agents}")

    orchestrator.start()


if __name__ == "__main__":
    main()
