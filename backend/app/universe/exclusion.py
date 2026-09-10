"""股票池排除规则（ExclusionEngine）。

可排除：
- trading_status = delisted（已退市，永久不可交易）
- trading_status = suspended（停牌中，不能成交，除非人工确认）
- 数据不完整：listing_date 太近或缺失（保守按不可交易处理）
- ST（用户可配置：默认不排除，但允许用户在配置中开关）
- 长期停牌：连续 N 个交易日无行情（占位，由 SnapshotService 在建立快照时
  通过 historical_bar 表查最长无成交日数）

不在此处排除：
- 价格异常 / 涨停 / 跌停（这是 trade 层规则，不是 universe）
- 个股风险事项（属于选股 alpha 信号）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Security, TradingDate, HistoricalBar


@dataclass(frozen=True)
class ExclusionDecision:
    """对单只证券的排除判定。"""

    symbol: str
    is_included: bool
    reason: str | None  # None 表示包含；非 None 表示被排除原因


class ExclusionEngine:
    """应用一组规则，把 Securities 转成 ExclusionDecision 列表。

    默认排除：
    - 已退市（trading_status == "delisted"）
    - 长期停牌（trading_status == "suspended"）
    - 上市日期缺失（数据不完整，按可观测风险处理）

    可选排除：
    - ST 股票（默认不排除，可通过 include_st=False 启用）
    """

    def __init__(
        self,
        db: Session,
        include_st: bool = True,
        missing_history_threshold_days: int = 30,
    ):
        self._db = db
        self._include_st = include_st
        self._missing_history_threshold = missing_history_threshold_days

    def evaluate(
        self,
        securities: Iterable[Security],
        as_of: date,
    ) -> list[ExclusionDecision]:
        decisions: list[ExclusionDecision] = []
        for sec in securities:
            reason = self._classify(sec, as_of)
            decisions.append(
                ExclusionDecision(
                    symbol=sec.symbol,
                    is_included=reason is None,
                    reason=reason,
                )
            )
        return decisions

    def _classify(self, sec: Security, as_of: date) -> str | None:
        if sec.trading_status == "delisted":
            return "delisted"
        if sec.delisted_date and sec.delisted_date <= as_of:
            return "delisted"
        if sec.trading_status == "suspended":
            return "suspended"
        if sec.listing_date is None:
            return "incomplete_data"
        if sec.listing_date > as_of:
            return "not_listed_yet"
        if not self._include_st and sec.is_st:
            return "st_excluded"
        return None


def find_long_suspension_symbols(
    db: Session,
    as_of: date,
    threshold_days: int = 30,
) -> set[str]:
    """扫描在 (as_of - threshold_days, as_of) 区间内 0 行情的股票。

    返回这些 symbol，给 ExclusionEngine 标记为 suspended-like。
    """
    trading_days = (
        db.execute(
            select(TradingDate.trade_date)
            .where(TradingDate.trade_date <= as_of)
            .order_by(TradingDate.trade_date.desc())
            .limit(threshold_days)
        )
        .scalars()
        .all()
    )
    if not trading_days:
        return set()

    active = set(
        db.execute(
            select(HistoricalBar.symbol)
            .where(HistoricalBar.trade_date.in_(trading_days))
            .distinct()
        )
        .scalars()
        .all()
    )
    all_symbols = set(
        db.execute(select(Security.symbol)).scalars().all()
    )
    return all_symbols - active
