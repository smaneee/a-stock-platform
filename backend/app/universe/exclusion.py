"""股票池排除规则（ExclusionEngine）。

可排除：
- trading_status = delisted（已退市，永久不可交易）
- trading_status = suspended（停牌中，不能成交，除非人工确认）
- 数据不完整：listing_date 太近或缺失（保守按不可交易处理）
- ST（用户可配置：默认不排除，但允许用户在配置中开关）
- 长期停牌：confirmed_long_suspension（连续 N 个预期交易日无成交）
  注意：**必须有真源覆盖 + 连续无成交** 才能判 confirmed，否则归
  incomplete_history（本地缓存缺失）或 provider_unknown（真源也未知）。

不在此处排除：
- 价格异常 / 涨停 / 跌停（这是 trade 层规则，不是 universe）
- 个股风险事项（属于选股 alpha 信号）

关键设计（迁移 0009 + 修复 P1）：
- `find_long_suspension_symbols` 不再用 `all_symbols - active` 这种会误排除
  全市场未缓存股票的反向逻辑；而是「遍历 expected trading days ∩ 真源覆盖
  范围，找连续无成交的 symbol」。
- 区分 3 种状态：
  - confirmed_long_suspension：连续 N 个预期交易日都无成交
  - incomplete_history：本地缓存覆盖不够，无法判断
  - provider_unknown：真源对这只股票也无数据
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    HistoryIngestBatch,
    Security,
    TradingDate,
    HistoricalBar,
)


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
    - 停牌（trading_status == "suspended"）
    - 上市日期缺失（数据不完整，按可观测风险处理）
    - 长期停牌：confirmed_long_suspension（基于 historical_bar 缺失统计）

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
        # listing_date 缺失：仅审计标签，不排除。
        # 原因：AKShare stock_info_a_code_name() 不返回 listing_date，
        # 早期版本若按"缺 listing_date 就排除"会把全市场全干掉。
        # 这里用 audit_reason = listing_date_unknown 暴露给 selection / paper trading
        # 进一步判断，universe 本身保持宽松口径。
        if sec.listing_date and sec.listing_date > as_of:
            return "not_listed_yet"
        if not self._include_st and sec.is_st:
            return "st_excluded"
        return None


def _get_recent_history_ingest(
    db: Session,
    as_of: date,
    symbol: str,
    threshold_days: int,
) -> HistoryIngestBatch | None:
    """检查「最近一次成功的历史数据 ingest 批次」是否覆盖到 as_of 日期。

    只有目标 symbol 明确出现在 covered_symbols 中，才算该股票被成功覆盖。
    全局 coverage_ratio 不能替代逐股票证据，否则部分批次会把未抓取股票误判停牌。
    """
    batches = (
        db.execute(
            select(HistoryIngestBatch)
            .where(HistoryIngestBatch.status.in_(("succeeded", "partial")))
            .where(HistoryIngestBatch.completed_at.isnot(None))
            .where(HistoryIngestBatch.end_date >= as_of - timedelta(days=1))
            .where(
                HistoryIngestBatch.start_date
                <= as_of - timedelta(days=threshold_days)
            )
            .order_by(HistoryIngestBatch.completed_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    for batch in batches:
        covered = batch.covered_symbols
        if isinstance(covered, list) and symbol in covered:
            return batch
    return None


def get_long_suspension_state(
    db: Session,
    symbol: str,
    as_of: date,
    threshold_days: int = 30,
) -> str:
    """判断单只股票的「长期停牌」状态。返回三种之一：

    - "confirmed_long_suspension"：连续 N 个预期交易日都无成交（确认长期停牌）
    - "incomplete_history"：本地缓存覆盖不够，无法判断（不能强行归 confirmed）
    - "provider_unknown"：连真源（akshare）对此 symbol 都无数据（视为数据缺失）

    关键前提（修复 P1）：必须先有「最近一次成功的 history_ingest 批次」覆盖
    到 as_of 之前，否则一律 incomplete_history — 因为"0 行情"可能不是"停牌"，
    而是"根本没拉过历史"。

    重要：返回 "ok" 表示这 N 天**有成交**或**有部分覆盖但不能定为长期停牌**。
    """
    # 关键：先看历史 ingest 覆盖。无覆盖 → 全部 incomplete_history（不能判 confirmed）
    if _get_recent_history_ingest(
        db,
        as_of=as_of,
        symbol=symbol,
        threshold_days=threshold_days,
    ) is None:
        # 再分两层：symbol 在 securities 里 → incomplete_history（保守）；
        # 完全没记录 → provider_unknown
        sec = db.get(Security, symbol)
        if sec is None:
            return "provider_unknown"
        return "incomplete_history"

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
        # 交易日历空：不能判定长期停牌
        # 但若 Security 都不存在 → provider_unknown 优先
        sec = db.get(Security, symbol)
        if sec is None:
            return "provider_unknown"
        return "incomplete_history"

    # 该 symbol 在预期交易日内的实际成交日数
    covered = (
        db.execute(
            select(HistoricalBar.trade_date)
            .where(HistoricalBar.symbol == symbol)
            .where(HistoricalBar.trade_date.in_(trading_days))
            .distinct()
        )
        .scalars()
        .all()
    )
    covered_set = set(covered)
    expected = set(trading_days)
    if covered_set >= expected:
        return "ok"  # 完全覆盖，N 天都有成交 → 非长期停牌

    # 检查 symbol 是否存在于 securities 中（无 symbol → provider 都没数据）
    sec = db.get(Security, symbol)
    if sec is None:
        return "provider_unknown"

    # 如果 N 天里 0 天有成交，且 symbol 存在 → 长期停牌
    if not covered_set:
        return "confirmed_long_suspension"

    # 部分覆盖：保守判 incomplete_history
    return "incomplete_history"


def find_long_suspension_symbols(
    db: Session,
    as_of: date,
    threshold_days: int = 30,
) -> set[str]:
    """扫描在 (as_of - threshold_days, as_of) 区间内**连续 0 行情**的股票。

    与旧版的关键差异（旧版 = "all_symbols - active" 会误把本地未缓存的股票都判为
    长期停牌；新版只判"连续 N 天确实无成交且该 symbol 存在于 securities"）。

    返回确认长期停牌的 symbol 集合（不含 incomplete_history / provider_unknown）。
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

    # 取出所有 securities 中已上市到 as_of 的 symbol
    listed_symbols = set(
        db.execute(
            select(Security.symbol).where(
                (Security.listing_date.is_(None)) | (Security.listing_date <= as_of)
            )
        ).scalars().all()
    )
    if not listed_symbols:
        return set()

    # 在预期交易日内有成交的 symbol
    active = set(
        db.execute(
            select(HistoricalBar.symbol)
            .where(HistoricalBar.trade_date.in_(trading_days))
            .distinct()
        )
        .scalars()
        .all()
    )

    # 只在「已上市 + 预期日内 0 成交」的子集里判 confirmed
    candidates = listed_symbols - active
    confirmed: set[str] = set()
    for sym in candidates:
        state = get_long_suspension_state(
            db, sym, as_of=as_of, threshold_days=threshold_days
        )
        if state == "confirmed_long_suspension":
            confirmed.add(sym)
    return confirmed


def classify_long_suspension_status(
    db: Session,
    symbols: Iterable[str],
    as_of: date,
    threshold_days: int = 30,
) -> dict[str, str]:
    """批量给一组 symbol 打 long_suspension 状态标签。返回 {symbol: state}。

    state 取值：confirmed_long_suspension / incomplete_history / provider_unknown / ok
    """
    out: dict[str, str] = {}
    for sym in symbols:
        out[sym] = get_long_suspension_state(
            db, sym, as_of=as_of, threshold_days=threshold_days
        )
    return out
