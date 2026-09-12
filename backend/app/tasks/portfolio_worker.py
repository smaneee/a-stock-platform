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

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database.models import PortfolioBacktest
from app.database.session import SessionLocal
from app.history.quality import QualityReport
from app.history.service import HistoricalDataService, HistoryResult
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
            histories: dict[str, HistoryResult] = {}
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

            # 2) 完整性校验：每个请求标的都必须 bars 非空 + is_complete=true + 覆盖请求区间
            allow_partial = bool(config_dict.get("allow_partial", False))
            requested_symbols = list(symbols)
            exclusion_reasons: dict[str, dict] = {}

            valid_histories: dict[str, list] = {}
            for sym in requested_symbols:
                result = histories.get(sym)
                reason = self._check_history_completeness(
                    sym, result, task.start_time, task.end_time
                )
                if reason is None:
                    valid_histories[sym] = result.bars
                else:
                    exclusion_reasons[sym] = reason

            # 基准完整性（独立校验，使用真实 HistoryResult）
            bench_reason = None
            if benchmark_symbol:
                bench_reason = self._check_history_completeness(
                    benchmark_symbol,
                    _wrap_history(benchmark_history, None),
                    task.start_time,
                    task.end_time,
                )

            # 默认必须全部完整，否则任务直接失败（不静默丢标的）
            if not valid_histories or (
                not allow_partial
                and (exclusion_reasons or bench_reason is not None)
            ):
                task.status = FAILED
                task.error_message = (
                    f"标的完整性校验失败：requested={len(requested_symbols)}"
                    f" completed={len(valid_histories)}"
                    + (
                        f" benchmark_incomplete={bench_reason}"
                        if bench_reason
                        else ""
                    )
                )
                task.result = json.dumps(
                    {
                        "requested_symbols": requested_symbols,
                        "executed_symbols": list(valid_histories.keys()),
                        "excluded_symbols": list(exclusion_reasons.keys()),
                        "exclusion_reasons": exclusion_reasons,
                        "benchmark_symbol": benchmark_symbol,
                        "benchmark_excluded_reason": bench_reason,
                    },
                    ensure_ascii=False,
                )
                task.finished_at = utc_now()
                db.commit()
                metrics.record_task_result(FAILED)
                return

            # 3) 构建策略 map：每个 symbol 同样使用 task.strategy_name
            # registry.get_strategy 返回 Strategy 实例（不是类），无需再实例化
            strategy_obj = registry.get_strategy(task.strategy_name)
            if strategy_obj is None:
                raise ValueError(f"策略不存在: {task.strategy_name}")
            strategies_map = {s: strategy_obj for s in valid_histories.keys()}

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
            result_obj = await asyncio.to_thread(engine.run, valid_histories)

            # 5) 再次取消检查
            db.refresh(task)
            if task.status == CANCELLED:
                return

            result_dict = result_obj.to_dict()
            # 把 requested/executed/excluded 信息写进结果，便于上游审计
            result_dict["requested_symbols"] = requested_symbols
            result_dict["executed_symbols"] = list(valid_histories.keys())
            result_dict["excluded_symbols"] = list(exclusion_reasons.keys())
            result_dict["exclusion_reasons"] = exclusion_reasons

            task.progress = 100
            task.status = SUCCEEDED
            task.result = json.dumps(result_dict, ensure_ascii=False)
            task.finished_at = utc_now()
            db.commit()
            metrics.record_task_result(SUCCEEDED)
            logger.info(
                "组合回测任务 #%d 成功，requested=%d executed=%d excluded=%d",
                task_id,
                len(requested_symbols),
                len(valid_histories),
                len(exclusion_reasons),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("组合回测 #%d 失败: %s", task_id, exc)
            db.rollback()
            self._mark_failed(task_id, str(exc))
        finally:
            db.close()

    @staticmethod
    def _check_history_completeness(
        symbol: str,
        result: "HistoryResult | None",
        start_time: datetime,
        end_time: datetime,
    ) -> dict | None:
        """检查单个标的的历史数据完整性。

        - bars 非空
        - is_complete=True
        - missing_dates 为空
        - 数据时间区间覆盖请求区间

        返回 None 表示通过；返回 dict 描述缺失原因（含 missing_dates）。
        """
        if result is None:
            return {"reason": "no_result", "missing_dates": []}
        if not result.bars:
            return {
                "reason": "empty_bars",
                "missing_dates": list(result.quality.missing_dates),
                "source": result.source,
                "is_complete": result.is_complete,
            }
        if not result.is_complete:
            return {
                "reason": "incomplete",
                "missing_dates": list(result.quality.missing_dates),
                "source": result.source,
            }
        dates = sorted(
            {
                b.market_time.date()
                for b in result.bars
                if b.market_time is not None
            }
        )
        if not dates:
            return {
                "reason": "no_dates",
                "missing_dates": [],
                "source": result.source,
            }
        first, last = dates[0], dates[-1]
        # 区间校验：首根 bar 日期不早于请求起始，最后一根不晚于请求结束
        # （数据可能因节假日跳过若干天，但跨度必须覆盖）
        if first < start_time.date() or last > end_time.date():
            return {
                "reason": "range_mismatch",
                "missing_dates": [],
                "actual_start": first.isoformat(),
                "actual_end": last.isoformat(),
                "requested_start": start_time.date().isoformat(),
                "requested_end": end_time.date().isoformat(),
                "source": result.source,
            }
        # 跨度必须 ≥ 1 天的请求跨度（防止缓存污染导致 range 不足）
        requested_span_days = (end_time.date() - start_time.date()).days
        if requested_span_days > 1:
            actual_span_days = (last - first).days
            if actual_span_days < requested_span_days - 1:
                return {
                    "reason": "range_too_short",
                    "missing_dates": [],
                    "actual_span_days": actual_span_days,
                    "requested_span_days": requested_span_days,
                    "source": result.source,
                }
        if result.quality.missing_dates:
            return {
                "reason": "missing_dates",
                "missing_dates": list(result.quality.missing_dates),
                "source": result.source,
            }
        return None

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


def _wrap_history(bars: list, source: str | None = None) -> HistoryResult:
    """把单纯的 bars 列表包成 HistoryResult，便于复用完整性校验逻辑。"""
    return HistoryResult(
        bars=bars or [],
        source=source or "cache",
        data_updated_at=None,
        is_complete=bool(bars),
        quality=QualityReport(total=len(bars or [])),
    )
