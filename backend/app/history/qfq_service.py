"""前复权回填任务的进程内调度。

与买点雷达样本外验证同一套写法：同一时刻只跑一个任务，状态放内存、报告不落库
（回填结果落在 ``historical_bars`` 里，状态本身是可重算的）。全市场一轮实测约
4~5 分钟，所以走后台任务 + 状态轮询，而不是阻塞 HTTP 请求。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from app.history.qfq_backfill import QfqBackfillReport, QfqBackfillService
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


class QfqBackfillRunner:
    """把 :class:`QfqBackfillService` 包成可轮询的后台任务。"""

    def __init__(self, service: QfqBackfillService | None = None) -> None:
        self._service = service or QfqBackfillService()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state = "idle"  # idle / running / done / failed
        self._done = 0
        self._total = 0
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None
        self._report: QfqBackfillReport | None = None

    @property
    def report(self) -> QfqBackfillReport | None:
        return self._report

    def status(self) -> dict[str, object]:
        """当前任务状态（前端轮询用）。"""
        return {
            "state": self._state,
            "progress": {"done": self._done, "total": self._total},
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "error": self._error,
            "has_report": self._report is not None,
            "report": self._report.as_dict() if self._report is not None else None,
        }

    async def start(self, symbols: Iterable[str] | None = None) -> dict[str, object]:
        """启动回填；已在运行则直接返回当前状态（幂等）。"""
        async with self._lock:
            if self._state == "running":
                return self.status()
            self._state = "running"
            self._done = 0
            self._total = 0
            self._error = None
            self._started_at = utc_now().isoformat()
            self._finished_at = None
            targets = list(symbols) if symbols is not None else None
            self._task = asyncio.create_task(
                self._run(targets), name="qfq-backfill"
            )
        return self.status()

    async def _run(self, symbols: list[str] | None) -> None:
        try:
            report = await asyncio.to_thread(
                self._service.run, symbols=symbols, progress=self._on_progress
            )
        except Exception as exc:  # noqa: BLE001 - 失败写进状态，不拖垮服务
            self._state = "failed"
            self._error = f"{type(exc).__name__}: {exc}"
            logger.exception("前复权回填失败")
        else:
            self._report = report
            self._state = "done"
        finally:
            self._finished_at = utc_now().isoformat()

    def _on_progress(self, done: int, total: int) -> None:
        self._done, self._total = done, total

    async def wait(self) -> QfqBackfillReport | None:
        """等待当前任务结束（测试与脚本用）。"""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self._report

    async def close(self) -> None:
        """应用关停时取消未完成的任务。"""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # 进程内单例由 main.py 组装，构造函数参数保留给测试注入
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"QfqBackfillRunner(state={self._state})"