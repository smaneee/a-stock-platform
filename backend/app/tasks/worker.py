"""后台回测任务 worker。

基于数据库的任务队列，不依赖 Redis/Celery：
- 服务启动时恢复遗留 running 任务为 queued
- 轮询 queued 任务并执行
- 支持取消（queued/running 均可取消）
- 保存进度、开始/结束时间、错误摘要
- 失败不导致主进程退出
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.backtest.engine import BacktestEngine
from app.config import get_settings
from app.database.models import Backtest
from app.database.session import SessionLocal
from app.history.limit_reference import attach_unadjusted_previous_close
from app.history.service import HistoricalDataService
from app.observability.metrics import metrics
from app.strategies import registry
from app.time_utils import utc_now
from app.tasks.status import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0


class BacktestWorker:
    """后台回测任务执行器。"""

    def __init__(self, session_factory: sessionmaker = SessionLocal, provider_manager=None):
        self._session_factory = session_factory
        self._provider_manager = provider_manager
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """启动 worker：先恢复遗留任务，再进入轮询循环。"""
        if self._running:
            return
        self._recover_stale_tasks()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="backtest-worker")
        logger.info("回测任务 worker 已启动")

    async def stop(self) -> None:
        """停止 worker。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("回测任务 worker 已停止")

    # ──────── 恢复 ────────

    def _recover_stale_tasks(self) -> int:
        """服务重启后，遗留 running 任务恢复为 queued。返回恢复数量。"""
        db = self._session_factory()
        try:
            stale = db.scalars(
                select(Backtest).where(Backtest.status == RUNNING)
            ).all()
            count = 0
            for task in stale:
                task.status = QUEUED
                task.started_at = None
                count += 1
            db.commit()
            if count:
                logger.warning("恢复 %d 个遗留 running 任务为 queued", count)
            return count
        finally:
            db.close()

    # ──────── 轮询 ────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                processed = await self._process_next()
                if not processed:
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("回测 worker 轮询异常（不影响主进程）: %s", exc)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _process_next(self) -> bool:
        """认领并执行一个 queued 任务，返回是否处理了任务。"""
        backtest_id = self._claim_next()
        if backtest_id is None:
            return False
        await self._execute(backtest_id)
        return True

    def _claim_next(self) -> int | None:
        """认领最早的一个 queued 任务，置为 running 并返回其 ID。"""
        db = self._session_factory()
        try:
            task = db.scalars(
                select(Backtest)
                .where(Backtest.status == QUEUED)
                .order_by(Backtest.id)
                .limit(1)
            ).first()
            if task is None:
                return None
            task.status = RUNNING
            task.started_at = utc_now()
            db.commit()
            return task.id
        finally:
            db.close()

    # ──────── 执行 ────────

    async def _execute(self, backtest_id: int) -> None:
        db = self._session_factory()
        try:
            backtest = db.get(Backtest, backtest_id)
            if backtest is None or backtest.status != RUNNING:
                return

            # 1) 获取历史数据（含缓存与增量同步）
            history_svc = HistoricalDataService(
                db, provider_manager=self._provider_manager
            )
            # D6：策略/收益用前复权，涨跌停判断用未复权昨收（两者不可混用）。
            # backtest_bars_adjust 设为 none 可一键回退到改造前的行为。
            bars_adjust = (get_settings().backtest_bars_adjust or "qfq").lower()
            history = await history_svc.get_history(
                backtest.symbol,
                backtest.start_time,
                backtest.end_time,
                adjust=bars_adjust,
            )
            limit_reference_missing = 0
            if bars_adjust != "none":
                # 另取未复权序列，仅用于还原昨收；取不到就保持 0（执行层跳过涨跌停）
                reference = await history_svc.get_history(
                    backtest.symbol,
                    backtest.start_time,
                    backtest.end_time,
                    adjust="none",
                )
                merged, limit_reference_missing = attach_unadjusted_previous_close(
                    history.bars, reference.bars
                )
                # HistoryResult 是 dataclass（不是 pydantic 模型），用 replace
                history = replace(history, bars=merged)

            # 2) 取消检查（历史数据获取可能耗时）
            db.refresh(backtest)
            if backtest.status == CANCELLED:
                return

            if not history.bars:
                backtest.status = FAILED
                backtest.error_message = "未获取到历史数据"
                backtest.finished_at = utc_now()
                db.commit()
                return

            # 3) 运行回测（CPU 密集，放入线程池避免阻塞事件循环）
            backtest.progress = 50
            db.commit()
            strategy = registry.get_strategy(backtest.strategy_name)
            if strategy is None:
                raise ValueError(f"策略不存在: {backtest.strategy_name}")
            engine = BacktestEngine(
                strategy=strategy,
                initial_cash=float(backtest.initial_cash),
                bars_adjust=bars_adjust,
            )
            result_obj = await asyncio.to_thread(engine.run, history.bars)

            # 4) 再次取消检查
            db.refresh(backtest)
            if backtest.status == CANCELLED:
                return

            result_dict = result_obj.to_dict()
            # D6：昨收还原诊断（>0 表示对应交易日的涨跌停判断已跳过）
            result_dict["limit_reference_missing"] = int(limit_reference_missing)
            result_dict["limit_reference_complete"] = (
                bars_adjust == "none" or limit_reference_missing == 0
            )
            if bars_adjust != "none" and limit_reference_missing:
                result_dict["limit_reference_note"] = (
                    f"有 {limit_reference_missing} 根 K 线未能对齐未复权昨收，"
                    "对应交易日的涨跌停判断已跳过（其余交易日正常）。"
                )

            backtest.progress = 100
            backtest.status = SUCCEEDED
            backtest.result = json.dumps(result_dict, ensure_ascii=False)
            backtest.finished_at = utc_now()
            db.commit()
            metrics.record_task_result(SUCCEEDED)
            logger.info("回测任务 #%d 成功", backtest_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("回测任务 #%d 失败: %s", backtest_id, exc)
            db.rollback()
            self._mark_failed(backtest_id, str(exc))
        finally:
            db.close()

    def _mark_failed(self, backtest_id: int, error: str) -> None:
        """标记任务失败（不覆盖已取消的任务）。"""
        db = self._session_factory()
        try:
            backtest = db.get(Backtest, backtest_id)
            if backtest is not None and backtest.status != CANCELLED:
                backtest.status = FAILED
                backtest.error_message = error[:500]
                backtest.finished_at = utc_now()
                db.commit()
                metrics.record_task_result(FAILED)
        finally:
            db.close()


def recover_stale_tasks() -> int:
    """独立函数：服务启动时恢复遗留 running 任务。"""
    worker = BacktestWorker()
    return worker._recover_stale_tasks()
