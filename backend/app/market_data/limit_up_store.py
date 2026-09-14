"""涨停板情绪池落库：把东财情绪池快照写成可回测的市场情绪因子。

两个层级：

- :class:`~app.database.models.LimitUpSentiment`：每个交易日一行汇总，
  含**封板率**与**连板高度**，直接作为回测的市场情绪因子。
- :class:`~app.database.models.LimitUpPoolMember`：当日各池明细，
  供「打板 / 连板接力」类个股策略回测使用。

抓取按 ``(交易日, 池)`` 幂等覆盖：同一天重复抓取不会产生重复行。
上游只保留最近若干个交易日，更早的数据拿不到，因此历史曲线需要每日累积。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.database.models import LimitUpPoolMember, LimitUpSentiment, TradingDate
from app.market_data.eastmoney_limit_up import (
    BEIJING,
    EastmoneyLimitUpService,
)
from app.market_rules.calendar import TradingCalendar
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

#: 东财实抓口径的算法版本（封板资金/封板时间/连板数来自上游榜单）
EASTMONEY_ALGORITHM_VERSION = "eastmoney-push2ex-v1"

# 情绪池 key -> 汇总行上的计数字段
POOL_COUNT_COLUMNS: dict[str, str] = {
    "limit-up": "limit_up_count",
    "limit-down": "limit_down_count",
    "broken-board": "broken_board_count",
    "strong": "strong_count",
    "sub-new": "sub_new_count",
}
# 涨停池里表示连板数的字段（1 = 首板）
STREAK_FIELD = "boards"


def _optional_float(raw: Any) -> float | None:
    """转 float；缺失返回 None（保留「无数据」语义）。"""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(raw: Any) -> int | None:
    value = _optional_float(raw)
    return None if value is None else int(value)


def _optional_bool(raw: Any) -> bool | None:
    text = "" if raw is None else str(raw).strip()
    if text in ("是", "1", "true", "True"):
        return True
    if text in ("否", "0", "false", "False"):
        return False
    return None


def _text(raw: Any, width: int) -> str:
    return ("" if raw is None else str(raw)).strip()[:width]


@dataclass(frozen=True)
class CaptureResult:
    """一次落库的结果摘要。"""

    trade_date: date
    pool_counts: dict[str, int] = field(default_factory=dict)
    member_count: int = 0
    max_streak: int = 0
    seal_rate: float | None = None


class LimitUpSentimentStore:
    """涨停板情绪池落库服务。"""

    def __init__(self, db: Session, service: EastmoneyLimitUpService):
        self._db = db
        self._service = service

    # ──────────────── 交易日对齐 ────────────────

    def _resolve_trade_date(self, requested: date | None) -> date:
        """把请求日期对齐到交易日。

        上游涨停池在非交易日仍会返回「最近一个交易日」的数据。如果直接拿
        自然日落库，周六 / 周日 / 节假日各会写出一条与前一交易日完全相同的
        幽灵点，把封板率曲线污染成锯齿。日历可用时统一回退到
        ``last_trading_day_on_or_before``；日历为空时保持原样（不阻断抓取）。
        """
        anchor = requested or datetime.now(BEIJING).date()
        calendar = TradingCalendar(self._db)
        if calendar.is_empty():
            return anchor
        try:
            return calendar.last_trading_day_on_or_before(anchor)
        except ValueError:
            # 日历覆盖不到该日期（过早 / 过晚），按原样处理
            return anchor

    def _trading_days_between(self, start: date, end: date) -> list[date]:
        """区间内的交易日；日历不可用时退化为「跳过周末」。"""
        calendar = TradingCalendar(self._db)
        if not calendar.is_empty():
            days = calendar.trading_days_in_range(start, end)
            if days:
                return sorted(days)
        return [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        ]

    def prune_non_trading_days(self) -> list[date]:
        """删除落在交易日历覆盖区间内、却不是交易日的情绪行。

        用于自愈早期 ``capture()`` 用自然日当 trade_date 留下的幽灵行。只在
        日历可用时执行，并且只清理日历**覆盖区间之内**的日期，避免误删日历
        尚未同步的历史区间。返回被清理的日期列表。
        """
        calendar = TradingCalendar(self._db)
        if calendar.is_empty():
            return []
        cal_min, cal_max = self._db.execute(
            select(func.min(TradingDate.trade_date), func.max(TradingDate.trade_date))
        ).one()
        if cal_min is None or cal_max is None:
            return []

        trading_days = calendar.trading_days_in_range(cal_min, cal_max)
        candidates = self._db.scalars(
            select(LimitUpSentiment.trade_date).where(
                LimitUpSentiment.trade_date >= cal_min,
                LimitUpSentiment.trade_date <= cal_max,
            )
        ).all()
        stale = sorted(d for d in candidates if d not in trading_days)
        if not stale:
            return []

        self._db.execute(
            delete(LimitUpPoolMember).where(LimitUpPoolMember.trade_date.in_(stale))
        )
        self._db.execute(
            delete(LimitUpSentiment).where(LimitUpSentiment.trade_date.in_(stale))
        )
        self._db.commit()
        logger.info("清理非交易日情绪行 %d 条: %s", len(stale), stale)
        return stale

    async def capture(self, trade_date: date | None = None) -> CaptureResult | None:
        """抓取指定交易日（默认北京时间的今天）的情绪池并落库。

        当天全部池都没有数据时返回 ``None``（超出上游保留窗口），不写入空行，
        避免污染情绪曲线。非交易日会自动对齐到最近一个交易日，因此周末 /
        节假日调用不会新增幽灵行。
        """
        # 自愈历史遗留的幽灵行（旧版本用自然日落库留下的非交易日记录）。
        # 放在抓取之前：即使上游暂时不可用，情绪曲线也能先被修正。
        self.prune_non_trading_days()
        target = self._resolve_trade_date(trade_date)
        results: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}
        for pool in POOL_COUNT_COLUMNS:
            result = await self._service.query_all(pool, trade_date=target)
            results[pool] = result.items
            counts[pool] = max(result.total, len(result.items))

        if not any(counts.values()):
            logger.info("情绪池 %s 无数据（超出上游保留窗口），跳过落库", target)
            return None

        limit_up_items = results.get("limit-up", [])
        streaks = [int(_optional_int(item.get(STREAK_FIELD)) or 0) for item in limit_up_items]
        denominator = counts["limit-up"] + counts["broken-board"]
        seal_rate = counts["limit-up"] / denominator if denominator else None
        broken_rate = counts["broken-board"] / denominator if denominator else None

        row = self._db.get(LimitUpSentiment, target)
        if row is None:
            row = LimitUpSentiment(trade_date=target)
            self._db.add(row)
        for pool, column in POOL_COUNT_COLUMNS.items():
            setattr(row, column, counts[pool])
        row.seal_rate = seal_rate
        row.broken_rate = broken_rate
        row.max_streak = max(streaks, default=0)
        row.first_board_count = sum(1 for value in streaks if value <= 1)
        row.streak_2_count = sum(1 for value in streaks if value == 2)
        row.streak_3_count = sum(1 for value in streaks if value == 3)
        row.streak_4_count = sum(1 for value in streaks if value == 4)
        row.streak_5plus_count = sum(1 for value in streaks if value >= 5)
        row.total_seal_amount = sum(
            _optional_float(item.get("seal_amount")) or 0.0 for item in limit_up_items
        )
        row.total_limit_up_amount = sum(
            _optional_float(item.get("amount")) or 0.0 for item in limit_up_items
        )
        row.source = "eastmoney"
        row.captured_at = utc_now()
        # P0-03：东财实抓同样登记样本面与算法版本，否则与 derived 无法区分口径
        covered = {
            str(item.get("symbol") or "").strip()
            for items in results.values()
            for item in items
        }
        covered.discard("")
        row.coverage_symbols = len(covered)
        row.algorithm_version = EASTMONEY_ALGORITHM_VERSION

        member_count = 0
        for pool, items in results.items():
            member_count += self._replace_members(target, pool, items)
        self._db.commit()
        logger.info(
            "情绪池落库完成：%s 涨停 %s / 炸板 %s / 封板率 %s / 最高连板 %s / 明细 %s 条",
            target,
            counts["limit-up"],
            counts["broken-board"],
            f"{seal_rate:.2%}" if seal_rate is not None else "-",
            row.max_streak,
            member_count,
        )
        return CaptureResult(
            trade_date=target,
            pool_counts=counts,
            member_count=member_count,
            max_streak=row.max_streak,
            seal_rate=seal_rate,
        )

    async def backfill(
        self, days: int = 20, *, end: date | None = None, delay: float = 0.4
    ) -> list[CaptureResult]:
        """回补最近 ``days`` 个自然日内的**交易日**；上游没有数据的日期自动跳过。

        目标日期来自交易日历，周末与节假日都不会发请求，也不会写出与前一
        交易日重复的幽灵行。
        """
        last = self._resolve_trade_date(end)
        captured: list[CaptureResult] = []
        for day in self._trading_days_between(last - timedelta(days=days), last):
            result = await self.capture(day)
            if result is not None:
                captured.append(result)
            if delay:
                await asyncio.sleep(delay)
        return captured

    def history(
        self,
        start: date | None = None,
        end: date | None = None,
        limit: int = 250,
    ) -> list[LimitUpSentiment]:
        """按交易日升序返回情绪因子曲线。"""
        stmt = select(LimitUpSentiment)
        if start is not None:
            stmt = stmt.where(LimitUpSentiment.trade_date >= start)
        if end is not None:
            stmt = stmt.where(LimitUpSentiment.trade_date <= end)
        stmt = stmt.order_by(LimitUpSentiment.trade_date.desc()).limit(max(1, limit))
        rows = list(self._db.scalars(stmt).all())
        rows.reverse()
        return rows

    def latest(self) -> LimitUpSentiment | None:
        return self._db.scalar(
            select(LimitUpSentiment)
            .order_by(LimitUpSentiment.trade_date.desc())
            .limit(1)
        )

    def has_data(self) -> bool:
        return self.latest() is not None

    def _replace_members(
        self, trade_date: date, pool: str, items: list[dict[str, Any]]
    ) -> int:
        """按 (交易日, 池) 覆盖写入明细，保证重复抓取幂等。"""
        self._db.execute(
            delete(LimitUpPoolMember).where(
                LimitUpPoolMember.trade_date == trade_date,
                LimitUpPoolMember.pool == pool,
            )
        )
        written = 0
        for item in items:
            symbol = _text(item.get("symbol"), 16)
            if not symbol:
                continue
            self._db.add(
                LimitUpPoolMember(
                    trade_date=trade_date,
                    pool=pool,
                    symbol=symbol,
                    name=_text(item.get("name"), 100),
                    price=_optional_float(item.get("price")),
                    change_pct=_optional_float(item.get("change_pct")),
                    limit_up_price=_optional_float(item.get("limit_up_price")),
                    seal_amount=_optional_float(item.get("seal_amount")),
                    amount=_optional_float(item.get("amount")),
                    turnover_rate=_optional_float(item.get("turnover_rate")),
                    boards=_optional_int(item.get("boards")),
                    first_seal_time=_text(item.get("first_seal_time"), 12),
                    last_seal_time=_text(item.get("last_seal_time"), 12),
                    limit_up_stat=_text(item.get("limit_up_stat"), 24),
                    industry=_text(item.get("industry"), 50),
                    is_new_high=_optional_bool(item.get("is_new_high")),
                    payload=item,
                    captured_at=utc_now(),
                )
            )
            written += 1
        return written
