"""Deterministic, point-in-time cross-sectional stock ranking."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    HistoricalBar,
    SelectionCandidate,
    SelectionRun,
    UniverseMember,
    UniverseSnapshot,
)


class SelectionError(ValueError):
    """Selection cannot produce a reliable result."""


class _BarRow(NamedTuple):
    """选股只用得到这 4 个日线字段。

    全市场一次排名要读约 127 万行，hydrate 成 ORM 实体是主要耗时
    （实测 36.9s 中 32.3s）。只取需要的列可跳过实体构造与 populate。
    """

    trade_date: date
    close: object
    amount: object
    fetched_at: datetime | None


@dataclass(frozen=True)
class SelectionConfig:
    top_n: int = 5
    min_bars: int = 61
    lookback_days: int = 180
    max_stale_days: int = 10
    exclude_st: bool = True
    adjust: str = "none"

    def validate(self) -> None:
        if not 1 <= self.top_n <= 50:
            raise SelectionError("top_n 必须在 1..50")
        if self.min_bars < 61:
            raise SelectionError("min_bars 至少为 61，才能计算 60 日动量")
        if self.lookback_days < self.min_bars:
            raise SelectionError("lookback_days 不能小于 min_bars")
        if not 1 <= self.max_stale_days <= 30:
            raise SelectionError("max_stale_days 必须在 1..30")
        if self.adjust not in {"none", "qfq", "hfq"}:
            raise SelectionError("adjust 仅支持 none/qfq/hfq")


@dataclass(frozen=True)
class SelectionCandidateView:
    symbol: str
    name: str
    exchange: str
    board: str
    rank: int
    score: float
    momentum_20: float
    momentum_60: float
    volatility_20: float
    max_drawdown_60: float
    average_amount_20: float
    last_price: float
    bar_count: int
    entry_date: date | None = None
    exit_date: date | None = None
    forward_return: float | None = None


@dataclass(frozen=True)
class SelectionResult:
    run_id: int
    trading_day: date
    total_candidates: int
    eligible_count: int
    candidates: list[SelectionCandidateView]
    evaluation_horizon: int | None = None
    evaluation_coverage: float | None = None
    mean_forward_return: float | None = None
    median_forward_return: float | None = None
    forward_win_rate: float | None = None
    evaluated_at: datetime | None = None


@dataclass(frozen=True)
class _Factors:
    symbol: str
    name: str
    exchange: str
    board: str
    momentum_20: float
    momentum_60: float
    volatility_20: float
    max_drawdown_60: float
    average_amount_20: float
    last_price: float
    bar_count: int
    last_date: date
    last_fetched_at: str


class SelectionService:
    """Rank only members and bars known at the requested trading day."""

    _WEIGHTS = {
        "momentum_20": 0.30,
        "momentum_60": 0.25,
        "volatility_20": 0.20,
        "max_drawdown_60": 0.15,
        "average_amount_20": 0.10,
    }

    def __init__(self, db: Session):
        self._db = db

    def rank(
        self,
        trading_day: date,
        config: SelectionConfig | None = None,
    ) -> SelectionResult:
        config = config or SelectionConfig()
        config.validate()
        snapshot = self._db.scalar(
            select(UniverseSnapshot).where(
                UniverseSnapshot.trading_day == trading_day
            )
        )
        if snapshot is None:
            raise SelectionError(f"交易日 {trading_day} 没有股票池快照")

        members = list(
            self._db.scalars(
                select(UniverseMember)
                .where(UniverseMember.snapshot_id == snapshot.id)
                .where(UniverseMember.is_included.is_(True))
                .where(UniverseMember.trading_status == "active")
                .order_by(UniverseMember.symbol)
            ).all()
        )
        if config.exclude_st:
            members = [member for member in members if not member.is_st]
        symbols = [member.symbol for member in members]
        members_by_symbol = {member.symbol: member for member in members}
        if not symbols:
            raise SelectionError("股票池没有满足基础交易约束的成员")

        bars_by_symbol = self._load_bars(symbols, trading_day, config)
        factors = [
            factor
            for symbol in symbols
            if (factor := self._calculate_factors(
                members_by_symbol[symbol],
                bars_by_symbol.get(symbol, []),
                trading_day,
                config,
            ))
            is not None
        ]
        if not factors:
            raise SelectionError(
                "没有股票具备足够且新鲜的历史数据，请先执行 history ingest"
            )

        ranked = self._score(factors)[: config.top_n]
        config_payload = asdict(config)
        config_payload["weights"] = dict(self._WEIGHTS)
        config_payload["data_fingerprint"] = self._fingerprint(factors)
        config_json = json.dumps(
            config_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()

        existing = self._db.scalar(
            select(SelectionRun).where(
                SelectionRun.snapshot_id == snapshot.id,
                SelectionRun.config_hash == config_hash,
            )
        )
        if existing is not None:
            return self._to_result(existing)

        run = SelectionRun(
            snapshot_id=snapshot.id,
            trading_day=trading_day,
            config_hash=config_hash,
            config_json=config_payload,
            total_candidates=len(symbols),
            eligible_count=len(factors),
        )
        self._db.add(run)
        self._db.flush()
        for candidate in ranked:
            self._db.add(
                SelectionCandidate(
                    run_id=run.id,
                    symbol=candidate.symbol,
                    name=candidate.name,
                    exchange=candidate.exchange,
                    board=candidate.board,
                    rank=candidate.rank,
                    score=candidate.score,
                    momentum_20=candidate.momentum_20,
                    momentum_60=candidate.momentum_60,
                    volatility_20=candidate.volatility_20,
                    max_drawdown_60=candidate.max_drawdown_60,
                    average_amount_20=candidate.average_amount_20,
                    last_price=candidate.last_price,
                    bar_count=candidate.bar_count,
                )
            )
        try:
            self._db.commit()
        except IntegrityError:
            # Two API requests may calculate the same immutable run concurrently.
            # The unique constraint is authoritative; return the winner.
            self._db.rollback()
            existing = self._db.scalar(
                select(SelectionRun).where(
                    SelectionRun.snapshot_id == snapshot.id,
                    SelectionRun.config_hash == config_hash,
                )
            )
            if existing is None:
                raise
            return self._to_result(existing)
        self._db.refresh(run)
        return self._to_result(run)

    def get_run(self, run_id: int) -> SelectionResult:
        run = self._db.get(SelectionRun, run_id)
        if run is None:
            raise SelectionError(f"选股运行 #{run_id} 不存在")
        return self._to_result(run)

    def _load_bars(
        self,
        symbols: list[str],
        trading_day: date,
        config: SelectionConfig,
    ) -> dict[str, list[_BarRow]]:
        grouped: dict[str, list[_BarRow]] = {}
        for offset in range(0, len(symbols), 500):
            chunk = symbols[offset : offset + 500]
            rows = self._db.execute(
                select(
                    HistoricalBar.symbol,
                    HistoricalBar.trade_date,
                    HistoricalBar.close,
                    HistoricalBar.amount,
                    HistoricalBar.fetched_at,
                )
                .where(HistoricalBar.symbol.in_(chunk))
                .where(HistoricalBar.trade_date <= trading_day)
                .where(
                    HistoricalBar.trade_date
                    >= trading_day - timedelta(days=config.lookback_days * 2)
                )
                .where(HistoricalBar.period == "daily")
                .where(HistoricalBar.adjust == config.adjust)
                .order_by(HistoricalBar.symbol, HistoricalBar.trade_date.desc())
            ).all()
            for symbol, trade_date, close, amount, fetched_at in rows:
                bucket = grouped.setdefault(symbol, [])
                if len(bucket) < config.lookback_days:
                    bucket.append(_BarRow(trade_date, close, amount, fetched_at))
        for bucket in grouped.values():
            bucket.reverse()
        return grouped

    @staticmethod
    def _calculate_factors(
        member: UniverseMember,
        bars: Sequence[_BarRow],
        trading_day: date,
        config: SelectionConfig,
    ) -> _Factors | None:
        if len(bars) < config.min_bars:
            return None
        last = bars[-1]
        if (trading_day - last.trade_date).days > config.max_stale_days:
            return None
        closes = [float(bar.close or 0) for bar in bars]
        if any(price <= 0 for price in closes[-61:]):
            return None
        returns = [
            closes[index] / closes[index - 1] - 1
            for index in range(len(closes) - 20, len(closes))
        ]
        recent_60 = closes[-60:]
        peak = recent_60[0]
        max_drawdown = 0.0
        for price in recent_60:
            peak = max(peak, price)
            max_drawdown = min(max_drawdown, price / peak - 1)
        amounts = [float(bar.amount or 0) for bar in bars[-20:]]
        fetched_at = last.fetched_at.isoformat() if last.fetched_at else ""
        return _Factors(
            symbol=member.symbol,
            name=member.name or member.symbol,
            exchange=member.exchange or "unknown",
            board=member.board or "unknown",
            momentum_20=closes[-1] / closes[-21] - 1,
            momentum_60=closes[-1] / closes[-61] - 1,
            volatility_20=statistics.pstdev(returns) * math.sqrt(252),
            max_drawdown_60=max_drawdown,
            average_amount_20=sum(amounts) / len(amounts),
            last_price=closes[-1],
            bar_count=len(bars),
            last_date=last.trade_date,
            last_fetched_at=fetched_at,
        )

    @classmethod
    def _score(cls, factors: list[_Factors]) -> list[SelectionCandidateView]:
        percentiles = {
            name: cls._percentiles(
                [getattr(item, name) for item in factors],
                higher_better=name != "volatility_20",
            )
            for name in cls._WEIGHTS
        }
        scored: list[tuple[_Factors, float]] = []
        for index, item in enumerate(factors):
            score = sum(
                cls._WEIGHTS[name] * percentiles[name][index]
                for name in cls._WEIGHTS
            )
            scored.append((item, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].symbol))
        return [
            SelectionCandidateView(
                symbol=item.symbol,
                name=item.name,
                exchange=item.exchange,
                board=item.board,
                rank=rank,
                score=round(score, 8),
                momentum_20=item.momentum_20,
                momentum_60=item.momentum_60,
                volatility_20=item.volatility_20,
                max_drawdown_60=item.max_drawdown_60,
                average_amount_20=item.average_amount_20,
                last_price=item.last_price,
                bar_count=item.bar_count,
            )
            for rank, (item, score) in enumerate(scored, start=1)
        ]

    @staticmethod
    def _percentiles(values: list[float], *, higher_better: bool) -> list[float]:
        if len(values) == 1:
            return [1.0]
        ordered = sorted(values)
        result = []
        denominator = len(values) - 1
        for value in values:
            first = bisect_left(ordered, value)
            last = bisect_right(ordered, value) - 1
            percentile = ((first + last) / 2) / denominator
            result.append(percentile if higher_better else 1 - percentile)
        return result

    @staticmethod
    def _fingerprint(factors: list[_Factors]) -> str:
        payload = [
            (item.symbol, item.last_date.isoformat(), item.last_fetched_at, item.bar_count)
            for item in sorted(factors, key=lambda value: value.symbol)
        ]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _to_result(run: SelectionRun) -> SelectionResult:
        candidates = [
            SelectionCandidateView(
                symbol=item.symbol,
                name=item.name,
                exchange=item.exchange,
                board=item.board,
                rank=item.rank,
                score=item.score,
                momentum_20=item.momentum_20,
                momentum_60=item.momentum_60,
                volatility_20=item.volatility_20,
                max_drawdown_60=item.max_drawdown_60,
                average_amount_20=item.average_amount_20,
                last_price=item.last_price,
                bar_count=item.bar_count,
                entry_date=item.entry_date,
                exit_date=item.exit_date,
                forward_return=item.forward_return,
            )
            for item in run.candidates
        ]
        return SelectionResult(
            run_id=run.id,
            trading_day=run.trading_day,
            total_candidates=run.total_candidates,
            eligible_count=run.eligible_count,
            candidates=candidates,
            evaluation_horizon=run.evaluation_horizon,
            evaluation_coverage=run.evaluation_coverage,
            mean_forward_return=run.mean_forward_return,
            median_forward_return=run.median_forward_return,
            forward_win_rate=run.forward_win_rate,
            evaluated_at=run.evaluated_at,
        )
