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
from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.backtest.execution import ExecutionConfig
from app.config import get_settings
from app.database.models import LimitUpSentiment, PortfolioBacktest
from app.database.session import SessionLocal
from app.history.limit_reference import attach_unadjusted_previous_close
from app.history.service import HistoricalDataService, HistoryResult
from app.observability.metrics import metrics
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine
from app.portfolio.sentiment import (
    SentimentGate,
    SentimentGateConfig,
    SentimentSnapshot,
)
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

# 任务行里成本字段为 NULL 时的兜底（迁移前的老行理论上可能为 NULL）
_DEFAULT_EXECUTION = ExecutionConfig()


def _iso_dates(values) -> list[str]:
    """把 missing_dates 统一成 ISO 字符串。

    DataQualityChecker 产出的是 ``datetime.date`` 对象，直接塞进任务结果会让
    ``json.dumps`` 抛 "Object of type date is not JSON serializable"，
    把「基准缺了哪些交易日」这条真正的失败原因整条吞掉。
    """
    result: list[str] = []
    for value in values or ():
        if isinstance(value, datetime):
            result.append(value.date().isoformat())
        elif isinstance(value, date):
            result.append(value.isoformat())
        else:
            result.append(str(value))
    return result


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
            benchmark_result: HistoryResult | None = None
            total = len(symbols) + (1 if benchmark_symbol else 0)
            done = 0
            # D6：策略/收益用前复权，涨跌停判断用未复权昨收（两者不可混用）。
            # get_settings 的 portfolio_bars_adjust 可切回 none（一键回退）。
            bars_adjust = (get_settings().portfolio_bars_adjust or "qfq").lower()
            limit_reference_missing = 0
            for sym in symbols:
                result = await history_svc.get_history(
                    sym, task.start_time, task.end_time, adjust=bars_adjust
                )
                if bars_adjust != "none":
                    # 另取未复权序列，仅用于还原昨收；取不到就保持 0（执行层跳过涨跌停）
                    reference = await history_svc.get_history(
                        sym, task.start_time, task.end_time, adjust="none"
                    )
                    merged, missing = attach_unadjusted_previous_close(
                        result.bars, reference.bars
                    )
                    limit_reference_missing += missing
                    # HistoryResult 是 dataclass（不是 pydantic 模型），用 replace 而不是 model_copy
                    result = replace(result, bars=merged)
                histories[sym] = result
                done += 1
                task.progress = int(done / max(total, 1) * 50)
                db.commit()

            if benchmark_symbol:
                benchmark_result = await history_svc.get_history(
                    benchmark_symbol, task.start_time, task.end_time
                )
                benchmark_history = benchmark_result.bars
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

            # 基准完整性（独立校验）。必须用真实的 HistoryResult：只有它带着
            # 交易日历算出的 expected_count，_wrap_history 出来的空报告会让
            # 「区间以节假日开头」的合法基准被 span 兜底规则误杀。
            bench_reason = None
            if benchmark_symbol:
                bench_reason = self._check_history_completeness(
                    benchmark_symbol,
                    benchmark_result,
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

            # 4) 组合回测配置：成本/滑点必须用任务里提交的参数，
            #    否则 API 上的 commission_rate / slippage 会被静默忽略。
            #    D5 的参与率上限同样来自 config_json（旧任务缺字段则用默认值）。
            exec_cfg = config_dict.get("execution")
            if not isinstance(exec_cfg, dict):
                exec_cfg = {}
            portfolio_config = PortfolioConfig(
                initial_cash=float(task.initial_cash),
                max_single_position=float(task.max_single_position),
                max_total_position=float(task.max_total_position),
                risk_free_rate=config_dict.get("risk_free_rate", 0.02),
                bars_adjust=bars_adjust,
                execution=ExecutionConfig(
                    commission_rate=float(
                        task.commission_rate
                        if task.commission_rate is not None
                        else _DEFAULT_EXECUTION.commission_rate
                    ),
                    slippage=float(
                        task.slippage
                        if task.slippage is not None
                        else _DEFAULT_EXECUTION.slippage
                    ),
                    max_participation_rate=float(
                        exec_cfg.get("max_participation_rate", 0.0) or 0.0
                    ),
                    allow_partial_fill=bool(exec_cfg.get("allow_partial_fill", True)),
                ),
            )

            engine = PortfolioBacktestEngine(
                strategies=strategies_map,
                weights=weights,
                config=portfolio_config,
                benchmark=benchmark_history,
                sentiment=self._build_sentiment_gate(
                    db, config_dict, task.start_time.date(), task.end_time.date()
                ),
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
            # D6：昨收还原诊断。>0 表示有 K 线的日期在未复权序列里找不到，
            # 这些日子执行层会跳过涨跌停判断（宁可漏判，不可错判）。
            result_dict["limit_reference_missing"] = int(limit_reference_missing)
            result_dict["limit_reference_complete"] = (
                bars_adjust == "none" or limit_reference_missing == 0
            )
            if bars_adjust != "none" and limit_reference_missing:
                result_dict["limit_reference_note"] = (
                    f"有 {limit_reference_missing} 根 K 线未能对齐未复权昨收，"
                    "对应交易日的涨跌停判断已跳过（其余交易日正常）。"
                )

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
    def _build_sentiment_gate(
        db, config_dict: dict, start: date, end: date
    ) -> SentimentGate | None:
        """按任务配置构建市场情绪闸门。

        - 未配置或未启用时返回 None（回测行为完全不变）。
        - 启用但区间内一条情绪数据都没有时**直接报错**，避免静默产出一份
          看起来有闸门、实际没生效的回测结果。
        - 只有区间内部分交易日缺数据时交给闸门按 ``on_missing`` 处理。
        """
        raw = config_dict.get("sentiment_gate")
        if not isinstance(raw, dict):
            return None
        known = set(SentimentGateConfig.__dataclass_fields__)
        gate_config = SentimentGateConfig(
            **{key: value for key, value in raw.items() if key in known}
        )
        if not gate_config.enabled:
            return None

        rows = db.scalars(
            select(LimitUpSentiment)
            .where(LimitUpSentiment.trade_date >= start)
            .where(LimitUpSentiment.trade_date <= end)
            .order_by(LimitUpSentiment.trade_date)
        ).all()
        if not rows:
            raise ValueError(
                "回测区间内没有涨停情绪数据，无法启用市场情绪闸门；请先调用 "
                "POST /api/market/limit-up/sentiment/backfill 回补历史情绪"
            )
        snapshots = [
            SentimentSnapshot(
                trade_date=row.trade_date,
                limit_up_count=row.limit_up_count,
                broken_board_count=row.broken_board_count,
                seal_rate=row.seal_rate,
                broken_rate=row.broken_rate,
                max_streak=row.max_streak,
            )
            for row in rows
        ]
        return SentimentGate(gate_config, snapshots)

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
                "missing_dates": _iso_dates(result.quality.missing_dates),
                "source": result.source,
                "is_complete": result.is_complete,
            }
        if not result.is_complete:
            return {
                "reason": "incomplete",
                "missing_dates": _iso_dates(result.quality.missing_dates),
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
        # 跨度粗校验仅作为「交易日历不可用」时的兜底。
        #
        # expected_count > 0 说明 DataQualityChecker 已经按交易日历逐日核对了
        # 缺口（missing_dates 为空 + actual_count 达标），此时再拿自然日跨度
        # 比较会把「请求区间以节假日开头/结尾」的正常数据误判为不完整：
        # 例如 2025-10-01 起（国庆休市至 10-08）的第一根 bar 必然是 10-09，
        # 自然日跨度天然比请求跨度少 8 天。
        expected_count = int(getattr(result.quality, "expected_count", 0) or 0)
        if expected_count <= 0:
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
                "missing_dates": _iso_dates(result.quality.missing_dates),
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
