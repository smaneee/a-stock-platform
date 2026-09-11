"""Recoverable daily pipeline from universe sync to paper rebalance plan."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import (
    DailyPipelineRun,
    HistoryIngestBatch,
    PaperAccount,
    UniverseMember,
    UniverseSnapshot,
)
from app.database.session import SessionLocal
from app.paper_trading.rebalance import PaperRebalanceService, RebalanceError
from app.selection import SelectionConfig, SelectionError, SelectionService
from app.tasks.status import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED
from app.time_utils import utc_now
from app.universe.sync_service import AllProvidersFailedError, UniverseSyncService

logger = logging.getLogger(__name__)

WAITING_HISTORY = "waiting_history"
PARTIAL = "partial"
_POLL_INTERVAL_SECONDS = 2.0


class DailyPipelineError(ValueError):
    """Daily pipeline cannot progress safely."""


class DailyPipelineService:
    """Create and advance persisted daily pipeline runs."""

    def __init__(self, db: Session, provider_manager=None):
        self._db = db
        self._provider_manager = provider_manager

    def create_run(
        self,
        trading_day: date,
        *,
        paper_account_id: int | None = None,
        auto_execute_paper: bool = False,
        paper_validation_override: bool = False,
        config: SelectionConfig | None = None,
        lookback_days: int = 365,
    ) -> DailyPipelineRun:
        if paper_account_id is not None and self._db.get(PaperAccount, paper_account_id) is None:
            raise DailyPipelineError("模拟账户不存在")
        if auto_execute_paper and paper_account_id is None:
            raise DailyPipelineError("自动执行模拟调仓必须指定 paper_account_id")
        config = config or SelectionConfig()
        config.validate()
        payload = {
            "selection": asdict(config),
            "history": {"lookback_days": lookback_days, "adjust": config.adjust},
            "paper": {"validation_override": paper_validation_override},
        }
        existing = self._db.scalar(
            select(DailyPipelineRun).where(
                DailyPipelineRun.trading_day == trading_day,
                DailyPipelineRun.paper_account_id == paper_account_id,
            )
        )
        if existing is not None:
            return existing
        run = DailyPipelineRun(
            trading_day=trading_day,
            status=QUEUED,
            stage="queued",
            paper_account_id=paper_account_id,
            auto_execute_paper=auto_execute_paper,
            config_json=payload,
            progress=0,
            updated_at=utc_now(),
        )
        self._db.add(run)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            winner = self._db.scalar(
                select(DailyPipelineRun).where(
                    DailyPipelineRun.trading_day == trading_day,
                    DailyPipelineRun.paper_account_id == paper_account_id,
                )
            )
            if winner is None:
                raise
            return winner
        self._db.refresh(run)
        return run

    async def advance(self, run_id: int) -> DailyPipelineRun:
        run = self._db.get(DailyPipelineRun, run_id)
        if run is None:
            raise DailyPipelineError("日常流水线任务不存在")
        if run.status in {SUCCEEDED, FAILED, CANCELLED}:
            return run
        try:
            run.status = RUNNING
            run.started_at = run.started_at or utc_now()
            run.error_message = None
            self._touch(run, "sync_universe", 5)
            await self._ensure_snapshot(run)

            self._touch(run, "history_ingest", 25)
            history_task = self._ensure_history_task(run)
            if history_task.status not in {SUCCEEDED, PARTIAL}:
                run.status = WAITING_HISTORY
                run.stage = "waiting_history"
                run.progress = max(run.progress, 35)
                run.updated_at = utc_now()
                self._db.commit()
                return run

            self._touch(run, "selection_rank", 65)
            selection_result = SelectionService(self._db).rank(
                run.trading_day, self._selection_config(run)
            )
            run.selection_run_id = selection_result.run_id

            self._touch(run, "paper_rebalance", 85)
            if run.paper_account_id is not None:
                await self._create_paper_plan(run)

            run.status = SUCCEEDED
            run.stage = "succeeded"
            run.progress = 100
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            self._db.commit()
            self._db.refresh(run)
            return run
        except (AllProvidersFailedError, DailyPipelineError, SelectionError, RebalanceError) as exc:
            self._fail(run, str(exc))
            return run

    async def _ensure_snapshot(self, run: DailyPipelineRun) -> UniverseSnapshot:
        snapshot = self._db.scalar(
            select(UniverseSnapshot).where(UniverseSnapshot.trading_day == run.trading_day)
        )
        if snapshot is not None:
            return snapshot
        result = await UniverseSyncService(self._db).sync(
            create_snapshot=True, trading_day=run.trading_day
        )
        snapshot = self._db.get(UniverseSnapshot, result.snapshot_id)
        if snapshot is None:
            raise DailyPipelineError("股票池同步完成但没有生成快照")
        return snapshot

    def _ensure_history_task(self, run: DailyPipelineRun) -> HistoryIngestBatch:
        if run.history_task_id:
            task = self._db.get(HistoryIngestBatch, run.history_task_id)
            if task is not None:
                return task
        snapshot = self._db.scalar(
            select(UniverseSnapshot).where(UniverseSnapshot.trading_day == run.trading_day)
        )
        if snapshot is None:
            raise DailyPipelineError("缺少股票池快照，无法创建历史入库任务")
        symbols = list(
            self._db.scalars(
                select(UniverseMember.symbol)
                .where(UniverseMember.snapshot_id == snapshot.id)
                .where(UniverseMember.is_included.is_(True))
                .where(UniverseMember.trading_status == "active")
                .order_by(UniverseMember.symbol)
            ).all()
        )
        if not symbols:
            raise DailyPipelineError("股票池没有可入库成员")
        history_cfg = dict(run.config_json.get("history") or {})
        task = HistoryIngestBatch(
            snapshot_id=snapshot.id,
            source="provider_manager",
            status=QUEUED,
            start_date=run.trading_day - timedelta(days=int(history_cfg.get("lookback_days", 365))),
            end_date=run.trading_day,
            adjust=str(history_cfg.get("adjust", "none")),
            requested_symbols=len(symbols),
            completed_symbols=0,
            total_bars=0,
            coverage_ratio=0.0,
            progress=0,
            cancel_requested=False,
            requested_symbol_list=symbols,
            covered_symbols=[],
            failed_symbols={},
        )
        self._db.add(task)
        self._db.flush()
        run.history_task_id = task.id
        self._db.commit()
        self._db.refresh(task)
        return task

    async def _create_paper_plan(self, run: DailyPipelineRun) -> None:
        if run.paper_account_id is None or run.selection_run_id is None:
            return
        service = PaperRebalanceService(self._db)
        symbols = service.required_symbols(run.paper_account_id, run.selection_run_id)
        if self._provider_manager is None:
            raise DailyPipelineError("缺少 provider_manager，无法获取调仓盘口")
        quotes = await self._provider_manager.get_quotes(symbols)
        plan = service.create_plan(
            run.paper_account_id,
            run.selection_run_id,
            quotes,
            validation_override=bool(
                (run.config_json.get("paper") or {}).get("validation_override", False)
            ),
        )
        run.paper_plan_id = plan.id
        if run.auto_execute_paper:
            executed = service.execute_plan(plan.id, quotes)
            run.paper_plan_id = executed.id

    def _selection_config(self, run: DailyPipelineRun) -> SelectionConfig:
        return SelectionConfig(**dict(run.config_json.get("selection") or {}))

    def _touch(self, run: DailyPipelineRun, stage: str, progress: int) -> None:
        run.stage = stage
        run.progress = max(run.progress, progress)
        run.updated_at = utc_now()
        self._db.commit()

    def _fail(self, run: DailyPipelineRun, message: str) -> None:
        run.status = FAILED
        run.error_message = message[:2000]
        run.finished_at = utc_now()
        run.updated_at = utc_now()
        self._db.commit()


class DailyPipelineWorker:
    """Poll daily pipeline runs and advance them when prerequisites are ready."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        provider_manager=None,
    ):
        self._session_factory = session_factory
        self._provider_manager = provider_manager
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._recover_stale_runs()
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="daily-pipeline-worker"
        )
        logger.info("日常流水线 worker 已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("日常流水线 worker 已停止")

    def _recover_stale_runs(self) -> int:
        db = self._session_factory()
        try:
            rows = db.scalars(
                select(DailyPipelineRun).where(DailyPipelineRun.status == RUNNING)
            ).all()
            for row in rows:
                row.status = QUEUED
                row.stage = "queued"
            db.commit()
            return len(rows)
        finally:
            db.close()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                run_id = self._claim_next()
                if run_id is None:
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                db = self._session_factory()
                try:
                    await DailyPipelineService(
                        db, provider_manager=self._provider_manager
                    ).advance(run_id)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("日常流水线 worker 异常: %s", exc)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _claim_next(self) -> int | None:
        db = self._session_factory()
        try:
            run = db.scalar(
                select(DailyPipelineRun)
                .where(DailyPipelineRun.status == QUEUED)
                .order_by(DailyPipelineRun.id)
                .limit(1)
            )
            if run is None:
                waiting = db.scalars(
                    select(DailyPipelineRun)
                    .where(DailyPipelineRun.status == WAITING_HISTORY)
                    .order_by(DailyPipelineRun.id)
                ).all()
                for candidate in waiting:
                    task = db.get(HistoryIngestBatch, candidate.history_task_id)
                    if task is not None and task.status in {
                        SUCCEEDED,
                        PARTIAL,
                        FAILED,
                        CANCELLED,
                    }:
                        run = candidate
                        break
                else:
                    return None
            if run.status == WAITING_HISTORY:
                task = db.get(HistoryIngestBatch, run.history_task_id)
                if task is None:
                    return None
                if task.status in {FAILED, CANCELLED}:
                    run.status = FAILED
                    run.error_message = f"历史入库任务 {task.status}"
                    run.finished_at = utc_now()
                    run.updated_at = utc_now()
                    db.commit()
                    return None
            run.status = RUNNING
            run.started_at = run.started_at or utc_now()
            run.updated_at = utc_now()
            db.commit()
            return run.id
        finally:
            db.close()


def serialize_pipeline_run(run: DailyPipelineRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "trading_day": run.trading_day.isoformat(),
        "status": run.status,
        "stage": run.stage,
        "paper_account_id": run.paper_account_id,
        "history_task_id": run.history_task_id,
        "selection_run_id": run.selection_run_id,
        "paper_plan_id": run.paper_plan_id,
        "config": run.config_json,
        "auto_execute_paper": run.auto_execute_paper,
        "progress": run.progress,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "updated_at": run.updated_at.isoformat(),
    }
