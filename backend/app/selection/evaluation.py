"""Out-of-sample evaluation for persisted selection runs."""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import HistoricalBar, SelectionCandidate, SelectionRun
from app.selection.service import SelectionError, SelectionResult, SelectionService
from app.time_utils import utc_now


@dataclass(frozen=True)
class SelectionEvaluationSummary:
    evaluated_runs: int
    candidate_observations: int
    mean_forward_return: float
    median_forward_return: float
    forward_win_rate: float
    average_coverage: float
    average_rank_ic: float | None
    average_turnover: float | None


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

    def summarize(
        self,
        *,
        limit: int = 50,
        min_coverage_ratio: float = 0.8,
        strategy_config: dict | None = None,
    ) -> SelectionEvaluationSummary:
        if not 1 <= limit <= 500:
            raise SelectionError("limit 必须在 1..500")
        if not 0.5 <= min_coverage_ratio <= 1.0:
            raise SelectionError("min_coverage_ratio 必须在 0.5..1.0")
        query_limit = 500 if strategy_config is not None else limit
        runs = list(
            self._db.scalars(
                select(SelectionRun)
                .where(SelectionRun.evaluated_at.is_not(None))
                .where(SelectionRun.evaluation_coverage >= min_coverage_ratio)
                .order_by(SelectionRun.trading_day.desc(), SelectionRun.id.desc())
                .limit(query_limit)
            ).all()
        )
        if strategy_config is not None:
            expected = self._stable_strategy_config(strategy_config)
            runs = [
                run
                for run in runs
                if self._stable_strategy_config(run.config_json) == expected
            ][:limit]
        if not runs:
            return SelectionEvaluationSummary(0, 0, 0.0, 0.0, 0.0, 0.0, None, None)

        returns: list[float] = []
        rank_ics: list[float] = []
        candidate_sets: list[set[str]] = []
        for run in reversed(runs):
            evaluated = [
                candidate
                for candidate in run.candidates
                if candidate.forward_return is not None
            ]
            returns.extend(float(item.forward_return) for item in evaluated)
            candidate_sets.append({item.symbol for item in run.candidates})
            rank_ic = self._spearman_rank_ic(evaluated)
            if rank_ic is not None:
                rank_ics.append(rank_ic)
        turnovers = [
            1 - len(previous & current) / min(len(previous), len(current))
            for previous, current in zip(candidate_sets, candidate_sets[1:])
            if previous and current
        ]
        return SelectionEvaluationSummary(
            evaluated_runs=len(runs),
            candidate_observations=len(returns),
            mean_forward_return=statistics.fmean(returns) if returns else 0.0,
            median_forward_return=statistics.median(returns) if returns else 0.0,
            forward_win_rate=(sum(value > 0 for value in returns) / len(returns))
            if returns
            else 0.0,
            average_coverage=statistics.fmean(
                float(run.evaluation_coverage or 0) for run in runs
            ),
            average_rank_ic=statistics.fmean(rank_ics) if rank_ics else None,
            average_turnover=statistics.fmean(turnovers) if turnovers else None,
        )

    @staticmethod
    def _stable_strategy_config(config: dict | str | None) -> str:
        if isinstance(config, str):
            config = json.loads(config)
        payload = dict(config or {})
        payload.pop("data_fingerprint", None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _spearman_rank_ic(candidates: list[SelectionCandidate]) -> float | None:
        if len(candidates) < 2:
            return None
        by_return = sorted(
            candidates,
            key=lambda item: (float(item.forward_return), item.symbol),
        )
        return_rank = {item.id: rank for rank, item in enumerate(by_return, start=1)}
        score_ranks = [float(-item.rank) for item in candidates]
        outcome_ranks = [float(return_rank[item.id]) for item in candidates]
        score_mean = statistics.fmean(score_ranks)
        outcome_mean = statistics.fmean(outcome_ranks)
        numerator = sum(
            (score - score_mean) * (outcome - outcome_mean)
            for score, outcome in zip(score_ranks, outcome_ranks)
        )
        denominator = (
            sum((score - score_mean) ** 2 for score in score_ranks)
            * sum((outcome - outcome_mean) ** 2 for outcome in outcome_ranks)
        ) ** 0.5
        return numerator / denominator if denominator else None
