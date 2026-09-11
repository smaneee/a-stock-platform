"""Recoverable background worker for universe-wide historical data ingestion."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.models import HistoryIngestBatch
from app.database.session import SessionLocal
from app.history.service import HistoricalDataService
from app.tasks.status import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

PARTIAL = "partial"
_POLL_INTERVAL_SECONDS = 1.0


class HistoryIngestWorker:
    """Execute persisted history jobs with bounded network concurrency."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        provider_manager=None,
        max_concurrency: int = 4,
    ):
        self._session_factory = session_factory
        self._provider_manager = provider_manager
        self._max_concurrency = max(1, min(max_concurrency, 16))
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._recover_stale_tasks()
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="history-ingest-worker"
        )
        logger.info("历史数据 ingest worker 已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("历史数据 ingest worker 已停止")

    def _recover_stale_tasks(self) -> int:
        db = self._session_factory()
        try:
            tasks = db.scalars(
                select(HistoryIngestBatch).where(
                    HistoryIngestBatch.status == RUNNING
                )
            ).all()
            for task in tasks:
                task.status = QUEUED
            db.commit()
            return len(tasks)
        finally:
            db.close()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                task_id = self._claim_next()
                if task_id is None:
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                await self._execute(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("历史数据 ingest worker 异常: %s", exc)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _claim_next(self) -> int | None:
        db = self._session_factory()
        try:
            task = db.scalar(
                select(HistoryIngestBatch)
                .where(HistoryIngestBatch.status == QUEUED)
                .order_by(HistoryIngestBatch.id)
                .limit(1)
            )
            if task is None:
                return None
            task.status = RUNNING
            task.started_at = task.started_at or utc_now()
            db.commit()
            return task.id
        finally:
            db.close()

    async def _execute(self, task_id: int) -> None:
        task_data = self._load_task(task_id)
        if task_data is None:
            return
        start_date, end_date, adjust, requested, already_covered = task_data
        pending = [symbol for symbol in requested if symbol not in already_covered]
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(symbol: str) -> tuple[str, bool, int, str | None]:
            async with semaphore:
                return await self._ingest_one(
                    symbol, start_date, end_date, adjust
                )

        futures = [asyncio.create_task(run_one(symbol)) for symbol in pending]
        try:
            for future in asyncio.as_completed(futures):
                if self._cancel_requested(task_id):
                    for outstanding in futures:
                        outstanding.cancel()
                    await asyncio.gather(*futures, return_exceptions=True)
                    self._finish_cancelled(task_id)
                    return
                symbol, complete, bar_count, error = await future
                self._record_progress(
                    task_id,
                    symbol=symbol,
                    complete=complete,
                    bar_count=bar_count,
                    error=error,
                )
        except asyncio.CancelledError:
            for future in futures:
                future.cancel()
            await asyncio.gather(*futures, return_exceptions=True)
            raise
        self._finish(task_id)

    def _load_task(
        self, task_id: int
    ) -> tuple[datetime, datetime, str, list[str], set[str]] | None:
        db = self._session_factory()
        try:
            task = db.get(HistoryIngestBatch, task_id)
            if task is None or task.status != RUNNING:
                return None
            requested = list(task.requested_symbol_list or [])
            covered = set(task.covered_symbols or [])
            return (
                datetime.combine(task.start_date, datetime.min.time()),
                datetime.combine(task.end_date, datetime.max.time()),
                task.adjust,
                requested,
                covered,
            )
        finally:
            db.close()

    async def _ingest_one(
        self, symbol: str, start: datetime, end: datetime, adjust: str
    ) -> tuple[str, bool, int, str | None]:
        db = self._session_factory()
        try:
            result = await HistoricalDataService(
                db, provider_manager=self._provider_manager
            ).get_history(symbol, start, end, adjust=adjust)
            if not result.bars:
                return symbol, False, 0, "provider returned no bars"
            if not result.is_complete:
                return (
                    symbol,
                    False,
                    len(result.bars),
                    f"incomplete history: {len(result.quality.missing_dates)} missing dates",
                )
            return symbol, True, len(result.bars), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("history ingest %s failed: %s", symbol, exc)
            return symbol, False, 0, str(exc)[:300]
        finally:
            db.close()

    def _cancel_requested(self, task_id: int) -> bool:
        db = self._session_factory()
        try:
            task = db.get(HistoryIngestBatch, task_id)
            return task is None or task.cancel_requested or task.status == CANCELLED
        finally:
            db.close()

    def _record_progress(
        self,
        task_id: int,
        *,
        symbol: str,
        complete: bool,
        bar_count: int,
        error: str | None,
    ) -> None:
        db = self._session_factory()
        try:
            task = db.get(HistoryIngestBatch, task_id)
            if task is None or task.status != RUNNING:
                return
            covered = list(task.covered_symbols or [])
            failures = dict(task.failed_symbols or {})
            if complete and symbol not in covered:
                covered.append(symbol)
                failures.pop(symbol, None)
            elif error:
                failures[symbol] = error[:300]
            task.covered_symbols = covered
            task.failed_symbols = failures
            task.completed_symbols = len(covered)
            if complete:
                task.total_bars += bar_count
            processed = len(covered) + len(failures)
            task.progress = min(
                99,
                int(processed * 100 / max(task.requested_symbols, 1)),
            )
            task.coverage_ratio = len(covered) / max(task.requested_symbols, 1)
            db.commit()
        finally:
            db.close()

    def _finish(self, task_id: int) -> None:
        db = self._session_factory()
        try:
            task = db.get(HistoryIngestBatch, task_id)
            if task is None:
                return
            if task.completed_symbols == task.requested_symbols:
                task.status = SUCCEEDED
                task.last_error = None
            elif task.completed_symbols > 0:
                task.status = PARTIAL
                task.last_error = (
                    f"{task.requested_symbols - task.completed_symbols} symbols incomplete"
                )
            else:
                task.status = FAILED
                task.last_error = "all symbols failed or incomplete"
            task.progress = 100
            task.completed_at = utc_now()
            db.commit()
        finally:
            db.close()

    def _finish_cancelled(self, task_id: int) -> None:
        db = self._session_factory()
        try:
            task = db.get(HistoryIngestBatch, task_id)
            if task is not None:
                task.status = CANCELLED
                task.completed_at = utc_now()
                db.commit()
        finally:
            db.close()
