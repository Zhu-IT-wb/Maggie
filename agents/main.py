#!/usr/bin/env python3
from __future__ import annotations

# 统一对外入口：优先走包导入，脚本直跑时退回相对目录导入。
try:
    from agents.s12_worktree_task_isolation import main
except ModuleNotFoundError:
    from s12_worktree_task_isolation import main


if __name__ == '__main__':
    main()
