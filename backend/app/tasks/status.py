"""后台任务状态常量。"""

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

# 终态（不再变化）
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})

# 可取消状态（尚未完成）
CANCELLABLE_STATES = frozenset({QUEUED, RUNNING})
