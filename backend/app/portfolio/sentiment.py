"""把涨停板情绪因子接进组合回测：市场情绪择时闸门。

情绪序列来自 ``limit_up_sentiment``（东财实时抓取或
:mod:`app.market_data.sentiment_backfill` 离线回算），本模块只做**纯计算**，
不访问数据库，便于单测与复用。

闸门语义
--------
- 只用"信号日之前"的情绪：当日情绪要收盘后才有，所以 ``lag_days`` 至少为 1，
  保证不引入未来函数。
- 闸门只拦**开新仓（BUY）**；SELL 始终放行，弱势市况下要能退出。
- ``scale_exposure=True`` 时按封板率线性缩放目标仓位（夹在
  ``[min_exposure, 1]``），实现"情绪好重仓、情绪差轻仓"。
- 区间内完全没有情绪数据时由 ``on_missing`` 决定：``allow`` 放行（并按
  ``exposure=1`` 处理），``block`` 一律拦截。注意"个别交易日缺数据"永远按
  ``on_missing`` 处理，只有"整个区间一条情绪数据都没有"才由调用方直接失败，
  避免静默产生一份没有闸门的回测结果。
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from typing import Literal

MISSING_DATA_REASON = "no_sentiment_data"


@dataclass(frozen=True)
class SentimentSnapshot:
    """闸门需要的单日情绪字段（与 ``LimitUpSentiment`` 解耦）。"""

    trade_date: date
    limit_up_count: int
    broken_board_count: int
    seal_rate: float | None
    broken_rate: float | None
    max_streak: int


@dataclass(frozen=True)
class SentimentGateConfig:
    """市场情绪闸门参数。"""

    enabled: bool = False
    lag_days: int = 1
    min_seal_rate: float | None = None
    max_broken_rate: float | None = None
    min_max_streak: int | None = None
    min_limit_up_count: int | None = None
    on_missing: Literal["allow", "block"] = "allow"
    scale_exposure: bool = False
    min_exposure: float = 0.3

    def validate(self) -> None:
        """参数自检，越界直接报错而不是静默按默认值跑。"""
        if self.lag_days < 1:
            raise ValueError("lag_days 至少为 1，否则会用到当日情绪（未来函数）")
        if self.on_missing not in {"allow", "block"}:
            raise ValueError("on_missing 只支持 allow/block")
        if not 0.0 <= self.min_exposure <= 1.0:
            raise ValueError("min_exposure 必须在 0..1")
        for name in ("min_seal_rate", "max_broken_rate"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 必须在 0..1")
        for name in ("min_max_streak", "min_limit_up_count"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} 不能为负")

    def has_threshold(self) -> bool:
        """是否配置了至少一个阈值条件。"""
        return any(
            getattr(self, name) is not None
            for name in (
                "min_seal_rate",
                "max_broken_rate",
                "min_max_streak",
                "min_limit_up_count",
            )
        )

    def to_dict(self) -> dict[str, object]:
        """序列化为接口返回结构。"""
        return {
            "enabled": self.enabled,
            "lag_days": self.lag_days,
            "min_seal_rate": self.min_seal_rate,
            "max_broken_rate": self.max_broken_rate,
            "min_max_streak": self.min_max_streak,
            "min_limit_up_count": self.min_limit_up_count,
            "on_missing": self.on_missing,
            "scale_exposure": self.scale_exposure,
            "min_exposure": self.min_exposure,
        }


@dataclass(frozen=True)
class GateDecision:
    """某个信号日的闸门判定结果。"""

    trade_date: date
    allowed: bool
    exposure: float
    sentiment_date: date | None = None
    seal_rate: float | None = None
    broken_rate: float | None = None
    max_streak: int | None = None
    limit_up_count: int | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """序列化为接口返回结构。"""
        return {
            "date": self.trade_date.isoformat(),
            "sentiment_date": (
                self.sentiment_date.isoformat() if self.sentiment_date else None
            ),
            "allowed": self.allowed,
            "exposure": self.exposure,
            "seal_rate": self.seal_rate,
            "broken_rate": self.broken_rate,
            "max_streak": self.max_streak,
            "limit_up_count": self.limit_up_count,
            "reasons": list(self.reasons),
        }


class SentimentGate:
    """按交易日给出"能否开新仓 + 仓位系数"。"""

    def __init__(
        self,
        config: SentimentGateConfig,
        snapshots: list[SentimentSnapshot] | dict[date, SentimentSnapshot],
    ) -> None:
        config.validate()
        self._config = config
        if isinstance(snapshots, dict):
            series = dict(snapshots)
        else:
            series = {item.trade_date: item for item in snapshots}
        self._series = series
        self._dates = sorted(series)

    @property
    def config(self) -> SentimentGateConfig:
        return self._config

    def __len__(self) -> int:
        return len(self._dates)

    @property
    def first_date(self) -> date | None:
        return self._dates[0] if self._dates else None

    @property
    def last_date(self) -> date | None:
        return self._dates[-1] if self._dates else None

    def decision(self, trade_date: date) -> GateDecision:
        """给出该信号日的闸门判定；情绪数据不足时按 ``on_missing`` 处理。"""
        index = bisect_left(self._dates, trade_date) - self._config.lag_days
        if index < 0:
            return self._missing_decision(trade_date)
        return self._evaluate(trade_date, self._series[self._dates[index]])

    # ---------------- 内部实现 ----------------

    def _missing_decision(self, trade_date: date) -> GateDecision:
        blocked = self._config.on_missing == "block"
        return GateDecision(
            trade_date=trade_date,
            allowed=not blocked,
            exposure=0.0 if blocked else 1.0,
            reasons=(MISSING_DATA_REASON,),
        )

    def _evaluate(
        self, trade_date: date, snapshot: SentimentSnapshot
    ) -> GateDecision:
        config = self._config
        reasons: list[str] = []
        if config.min_seal_rate is not None and (
            snapshot.seal_rate is None or snapshot.seal_rate < config.min_seal_rate
        ):
            reasons.append("seal_rate_below_min")
        if config.max_broken_rate is not None and (
            snapshot.broken_rate is None
            or snapshot.broken_rate > config.max_broken_rate
        ):
            reasons.append("broken_rate_above_max")
        if (
            config.min_max_streak is not None
            and snapshot.max_streak < config.min_max_streak
        ):
            reasons.append("max_streak_below_min")
        if (
            config.min_limit_up_count is not None
            and snapshot.limit_up_count < config.min_limit_up_count
        ):
            reasons.append("limit_up_count_below_min")

        allowed = not reasons
        return GateDecision(
            trade_date=trade_date,
            allowed=allowed,
            exposure=self._exposure(snapshot) if allowed else 0.0,
            sentiment_date=snapshot.trade_date,
            seal_rate=snapshot.seal_rate,
            broken_rate=snapshot.broken_rate,
            max_streak=snapshot.max_streak,
            limit_up_count=snapshot.limit_up_count,
            reasons=tuple(reasons),
        )

    def _exposure(self, snapshot: SentimentSnapshot) -> float:
        """仓位系数：不缩放时为 1；缩放时按封板率线性映射到 [min_exposure, 1]。"""
        config = self._config
        if not config.scale_exposure:
            return 1.0
        if snapshot.seal_rate is None:
            return config.min_exposure
        return min(1.0, max(config.min_exposure, snapshot.seal_rate))
