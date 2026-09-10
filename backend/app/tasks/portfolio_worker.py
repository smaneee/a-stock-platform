"""组合回测后台任务 worker。

复用 BacktestWorker 的轮询/恢复/取消模式，针对 PortfolioBacktest 表。
- 认领最早的 queued 任务（按 id）。
- 拉取每个 symbol 的历史（含 benchmark），调用 PortfolioBacktestEngine。
- 保存进度、结果、错误摘要。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.models import PortfolioBacktest
from app.database.session import SessionLocal
from app.history.service import HistoricalDataService
from app.observability.metrics import metrics
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine
from app.strategies import registry
from app.tasks.status import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
)
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0


class PortfolioBacktestWorker:
    """组合回测后台任务执行器。"""

    def __init__(self, session_factory: sessionmaker = SessionLocal, provider_manager=None):
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
        self._recover_stale_tasks()
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="portfolio-backtest-worker"
        )
        logger.info("组合回测任务 worker 已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("组合回测任务 worker 已停止")

    # ──────── 恢复 ────────

    def _recover_stale_tasks(self) -> int:
        """服务重启后将遗留 running 任务恢复为 queued。"""
        db = self._session_factory()
        try:
            stale = db.scalars(
                select(PortfolioBacktest).where(
                    PortfolioBacktest.status == RUNNING
                )
            ).all()
            count = 0
            for task in stale:
                task.status = QUEUED
                task.started_at = None
                count += 1
            db.commit()
            if count:
                logger.warning("恢复 %d 个组合回测遗留任务", count)
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
                logger.error("组合回测轮询异常（不影响主进程）: %s", exc)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _process_next(self) -> bool:
        task_id = self._claim_next()
        if task_id is None:
            return False
        await self._execute(task_id)
        return True

    def _claim_next(self) -> int | None:
        db = self._session_factory()
        try:
            task = db.scalars(
                select(PortfolioBacktest)
                .where(PortfolioBacktest.status == QUEUED)
                .order_by(PortfolioBacktest.id)
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

    async def _execute(self, task_id: int) -> None:
        db = self._session_factory()
        try:
            task = db.get(PortfolioBacktest, task_id)
            if task is None or task.status != RUNNING:
                return

            symbols = json.loads(task.symbols)
            weights = json.loads(task.weights) if task.weights else None
            benchmark_symbol = task.benchmark_symbol
            config_dict = json.loads(task.config_json) if task.config_json else {}

            # 取消检查（启动准备阶段很快，这里顺带检查）
            db.refresh(task)
            if task.status == CANCELLED:
                return

            # 1) 拉取所有 symbol 的历史行情（含 benchmark）
            history_svc = HistoricalDataService(
                db, provider_manager=self._provider_manager
            )
            histories: dict[str, list] = {}
            benchmark_history: list = []
            total = len(symbols) + (1 if benchmark_symbol else 0)
            done = 0
            for sym in symbols:
                histories[sym] = await history_svc.get_history(
                    sym, task.start_time, task.end_time
                )
                done += 1
                task.progress = int(done / max(total, 1) * 50)
                db.commit()

            if benchmark_symbol:
                bench_data = await history_svc.get_history(
                    benchmark_symbol, task.start_time, task.end_time
                )
                benchmark_history = bench_data.bars
                done += 1
                task.progress = int(done / max(total, 1) * 50)
                db.commit()

            # 取消检查
            db.refresh(task)
            if task.status == CANCELLED:
                return

            # 2) 过滤空数据
            histories = {s: h.bars for s, h in histories.items() if h.bars}
            if not histories:
                task.status = FAILED
                task.error_message = "所有标的均未获取到历史数据"
                task.finished_at = utc_now()
                db.commit()
                return

            # 3) 构建策略 map：每个 symbol 同样使用 task.strategy_name
            # registry.get_strategy 返回 Strategy 实例（不是类），无需再实例化
            strategy_obj = registry.get_strategy(task.strategy_name)
            if strategy_obj is None:
                raise ValueError(f"策略不存在: {task.strategy_name}")
            strategies_map = {s: strategy_obj for s in histories.keys()}

            # 4) 组合回测配置
            portfolio_config = PortfolioConfig(
                initial_cash=float(task.initial_cash),
                max_single_position=float(task.max_single_position),
                max_total_position=float(task.max_total_position),
                risk_free_rate=config_dict.get("risk_free_rate", 0.02),
            )

            engine = PortfolioBacktestEngine(
                strategies=strategies_map,
                weights=weights,
                config=portfolio_config,
                benchmark=benchmark_history,
            )

            # CPU 密集 → 线程池
            result_obj = await asyncio.to_thread(engine.run, histories)

            # 5) 再次取消检查
            db.refresh(task)
            if task.status == CANCELLED:
                return

            task.progress = 100
            task.status = SUCCEEDED
            task.result = json.dumps(result_obj.to_dict(), ensure_ascii=False)
            task.finished_at = utc_now()
            db.commit()
            metrics.record_task_result(SUCCEEDED)
            logger.info("组合回测任务 #%d 成功，标的数=%d", task_id, len(histories))
        except Exception as exc:  # noqa: BLE001
            logger.error("组合回测 #%d 失败: %s", task_id, exc)
            db.rollback()
            self._mark_failed(task_id, str(exc))
        finally:
            db.close()

    def _mark_failed(self, task_id: int, error: str) -> None:
        db = self._session_factory()
        try:
            task = db.get(PortfolioBacktest, task_id)
            if task is not None and task.status != CANCELLED:
                task.status = FAILED
                task.error_message = error[:500]
                task.finished_at = utc_now()
                db.commit()
                metrics.record_task_result(FAILED)
        finally:
            db.close()


def recover_stale_portfolio_tasks() -> int:
    """启动时恢复遗留 PortfolioBacktest running 任务。"""
    return PortfolioBacktestWorker()._recover_stale_tasks()
