"""Out-of-sample evaluation for persisted selection runs."""
from __future__ import annotations

import json
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import HistoricalBar, SelectionCandidate, SelectionRun
from app.selection.service import SelectionError, SelectionResult, SelectionService
from app.time_utils import utc_now


class SelectionEvaluationService:
    """Evaluate candidates without using information available on selection day."""

    def __init__(self, db: Session):
        self._db = db

    def evaluate(
        self,
        run_id: int,
        *,
        horizon_days: int = 20,
        min_coverage_ratio: float = 0.8,
    ) -> SelectionResult:
        if not 1 <= horizon_days <= 120:
            raise SelectionError("horizon_days 必须在 1..120")
        if not 0.5 <= min_coverage_ratio <= 1.0:
            raise SelectionError("min_coverage_ratio 必须在 0.5..1.0")

        run = self._db.get(SelectionRun, run_id)
        if run is None:
            raise SelectionError(f"选股运行 #{run_id} 不存在")
        candidates = list(run.candidates)
        if not candidates:
            raise SelectionError("选股运行没有候选股票")

        config = run.config_json
        if isinstance(config, str):
            config = json.loads(config)
        adjust = (config or {}).get("adjust", "none")
        symbols = [candidate.symbol for candidate in candidates]
        rows = self._db.scalars(
            select(HistoricalBar)
            .where(HistoricalBar.symbol.in_(symbols))
            .where(HistoricalBar.trade_date > run.trading_day)
            .where(HistoricalBar.period == "daily")
            .where(HistoricalBar.adjust == adjust)
            .order_by(HistoricalBar.symbol, HistoricalBar.trade_date)
        ).all()
        grouped: dict[str, list[HistoricalBar]] = {}
        for row in rows:
            grouped.setdefault(row.symbol, []).append(row)

        evaluated: list[tuple[SelectionCandidate, HistoricalBar, HistoricalBar, float]] = []
        for candidate in candidates:
            future = grouped.get(candidate.symbol, [])
            if len(future) < horizon_days:
                continue
            entry = future[0]
            exit_bar = future[horizon_days - 1]
            entry_price = float(entry.open or 0)
            exit_price = float(exit_bar.close or 0)
            if entry_price <= 0 or exit_price <= 0:
                continue
            evaluated.append(
                (candidate, entry, exit_bar, exit_price / entry_price - 1)
            )

        coverage = len(evaluated) / len(candidates)
        if coverage < min_coverage_ratio:
            raise SelectionError(
                f"未来行情覆盖率 {coverage:.1%} 低于最低要求 {min_coverage_ratio:.1%}"
            )

        returns = [item[3] for item in evaluated]
        for candidate, entry, exit_bar, forward_return in evaluated:
            candidate.entry_date = entry.trade_date
            candidate.exit_date = exit_bar.trade_date
            candidate.forward_return = forward_return
        run.evaluation_horizon = horizon_days
        run.evaluation_coverage = coverage
        run.mean_forward_return = statistics.fmean(returns)
        run.median_forward_return = statistics.median(returns)
        run.forward_win_rate = sum(value > 0 for value in returns) / len(returns)
        run.evaluated_at = utc_now()
        self._db.commit()
        return SelectionService(self._db).get_run(run.id)
