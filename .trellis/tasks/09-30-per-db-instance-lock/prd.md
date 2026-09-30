# 按库的文件句柄锁：watcher 单实例互斥

## Goal

runtime_dir() 下按库加锁（fcntl/msvcrt），抢不到即退出；heartbeat 报出实例数

## Requirements

- TBD

## Acceptance Criteria

- [ ] TBD

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
