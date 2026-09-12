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

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database.models import LimitUpPoolMember, LimitUpSentiment
from app.market_data.eastmoney_limit_up import (
    BEIJING,
    EastmoneyLimitUpService,
)
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

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

    async def capture(self, trade_date: date | None = None) -> CaptureResult | None:
        """抓取指定交易日（默认北京时间的今天）的情绪池并落库。

        当天全部池都没有数据时返回 ``None``（非交易日或超出上游保留窗口），
        不写入空行，避免污染情绪曲线。
        """
        target = trade_date or datetime.now(BEIJING).date()
        results: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}
        for pool in POOL_COUNT_COLUMNS:
            result = await self._service.query_all(pool, trade_date=target)
            results[pool] = result.items
            counts[pool] = max(result.total, len(result.items))

        if not any(counts.values()):
            logger.info("情绪池 %s 无数据（非交易日或超出上游保留窗口），跳过落库", target)
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
        """回补最近 ``days`` 个自然日；上游没有数据的日期自动跳过。"""
        last = end or datetime.now(BEIJING).date()
        captured: list[CaptureResult] = []
        for offset in range(days, -1, -1):
            day = last - timedelta(days=offset)
            if day.weekday() >= 5:  # 周末必定没有情绪池数据，省掉请求
                continue
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
