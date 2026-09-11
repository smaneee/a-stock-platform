"""交易日历服务。

数据源（按优先级降级）：
1. AKShare 实数据源：通过 `ak.tool_trade_date_hist_sina()` 同步最权威数据。
2. exchange_calendars XSHG 基准日历：作为算法降级，避免 AKShare 不可用时无法启动。
3. 本地数据库缓存：所有来源最终落库 `trading_calendar` 表。

启动策略：
- 进程启动时若本地交易日历为空，触发 AKShare 同步；
- AKShare 失败时使用 exchange_calendars 兜底；
- 兜底后仍为空则 readiness 探针返回 503，禁止静默启动。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import TradingDate

logger = logging.getLogger(__name__)

# 节假日最长连续休市约 8 天（春节），留足余量
_MAX_LOOKAHEAD_DAYS = 30

# 启动时同步的窗口天数
_DEFAULT_SYNC_LOOKBACK_DAYS = 365
_DEFAULT_SYNC_LOOKFORWARD_DAYS = 180


class TradingCalendar:
    """交易日历查询服务。

    每个实例绑定一个 Session，缓存已查询日期以减少重复 DB 访问。
    """

    def __init__(self, db: Session):
        self._db = db
        self._cache: dict[date, bool] = {}
        self._range_cache: dict[tuple, set[date]] = {}

    def is_trading_day(self, d: date) -> bool:
        """判断某日是否为交易日。"""
        if d in self._cache:
            return self._cache[d]
        row = self._db.get(TradingDate, d)
        result = row is not None
        self._cache[d] = result
        return result

    def trading_days_in_range(self, start: date, end: date) -> set[date]:
        """返回 [start, end] 区间内（闭区间）的所有交易日。

        利用单次 DB 查询批量读取范围内记录，避免逐日判断的 N 次查询。
        """
        key = (start, end)
        if key in self._range_cache:
            return set(self._range_cache[key])
        rows = self._db.scalars(
            select(TradingDate.trade_date).where(
                TradingDate.trade_date >= start,
                TradingDate.trade_date <= end,
            )
        ).all()
        result = set(rows)
        self._range_cache[key] = result
        return result

    def next_trading_day(self, d: date) -> date:
        """返回 d 之后（不含 d）的下一个交易日。"""
        cur = d + timedelta(days=1)
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur += timedelta(days=1)
        raise ValueError("30 天内未找到下一交易日，交易日历可能未同步")

    def prev_trading_day(self, d: date) -> date:
        """返回 d 之前（不含 d）的上一个交易日。"""
        cur = d - timedelta(days=1)
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise ValueError("30 天内未找到上一交易日，交易日历可能未同步")

    def last_trading_day_on_or_before(self, d: date) -> date:
        """返回不晚于 d 的最近交易日（含 d 自身）。"""
        if self.is_trading_day(d):
            return d
        return self.prev_trading_day(d)

    def count(self) -> int:
        """返回本地缓存中的交易日总数。"""
        return self._db.query(TradingDate).count()

    def is_empty(self) -> bool:
        """本地缓存是否为空。"""
        return self.count() == 0


def _xshg_sessions(calendar, start: date, end: date) -> list[date]:
    """从 exchange_calendars 的 XSHG 日历取 [start, end] 内的交易日。

    exchange_calendars >= 4 只提供 sessions / sessions_in_range；更早的
    pandas_market_calendars 风格才有 valid_days。这里两种都兼容，避免
    兜底源因为 API 变更而静默失效（实测 4.13.2 没有 valid_days）。

    另外必须把区间裁剪进日历自身的边界：XSHG 日历只到 2026-12-31，
    默认 lookahead 180 天会越界并触发 DateOutOfBounds。
    """
    bound_start = getattr(calendar, "first_session", None)
    bound_end = getattr(calendar, "last_session", None)
    if hasattr(bound_start, "date"):
        start = max(start, bound_start.date())
    if hasattr(bound_end, "date"):
        end = min(end, bound_end.date())
    if start > end:
        return []

    sessions_in_range = getattr(calendar, "sessions_in_range", None)
    if callable(sessions_in_range):
        sessions = sessions_in_range(start.isoformat(), end.isoformat())
        return [d.date() if hasattr(d, "date") else d for d in sessions]

    valid_days = getattr(calendar, "valid_days", None)
    if valid_days is None:
        raise RuntimeError(
            "exchange_calendars 版本既不支持 sessions_in_range 也没有 valid_days"
        )
    from pandas import Timestamp

    mask = (valid_days >= Timestamp(start)) & (valid_days <= Timestamp(end))
    return [d.date() if hasattr(d, "date") else d for d in valid_days[mask]]


def sync_trading_calendar(
    db: Session,
    lookback_days: int = _DEFAULT_SYNC_LOOKBACK_DAYS,
    lookahead_days: int = _DEFAULT_SYNC_LOOKFORWARD_DAYS,
) -> dict:
    """多源同步交易日历到本地数据库。

    优先级：AKShare → exchange_calendars XSHG → 本地缓存。
    返回 {source, added, total}，source 为实际填充来源。
    """
    added = 0
    source = "none"

    # 数据源 1：AKShare
    try:
        import akshare as ak  # type: ignore

        df = ak.tool_trade_date_hist_sina()
        if df is not None and not df.empty and "trade_date" in df.columns:
            for raw in df["trade_date"].tolist():
                try:
                    d = raw.date() if hasattr(raw, "date") else date.fromisoformat(str(raw))
                except (ValueError, TypeError):
                    continue
                if not _in_window(d, lookback_days, lookahead_days):
                    continue
                if db.get(TradingDate, d) is None:
                    db.add(TradingDate(trade_date=d))
                    added += 1
            db.commit()
            source = "akshare"
            logger.info("交易日历同步：AKShare 新增 %d 条", added)
            return {"source": source, "added": added, "total": _count(db)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AKShare 同步失败，转 exchange_calendars 兜底: %s", exc)

    # 数据源 2：exchange_calendars XSHG 兜底
    try:
        import exchange_calendars as ec  # type: ignore

        xshg = ec.get_calendar("XSHG")
        start = date.today() - timedelta(days=lookback_days)
        end = date.today() + timedelta(days=lookahead_days)
        session_dates = _xshg_sessions(xshg, start, end)
        for d in session_dates:
            if db.get(TradingDate, d) is None:
                db.add(TradingDate(trade_date=d))
                added += 1
        db.commit()
        if added > 0:
            source = "exchange_calendars"
            logger.info("交易日历同步：exchange_calendars XSHG 新增 %d 条", added)
        else:
            source = "exchange_calendars"
            logger.info("交易日历同步：exchange_calendars 已存在，无新增")
        return {"source": source, "added": added, "total": _count(db)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange_calendars 同步也失败: %s", exc)

    # 数据源 3：本地缓存（无新增）
    total = _count(db)
    if total > 0:
        return {"source": "local_cache", "added": 0, "total": total}

    return {"source": "empty", "added": 0, "total": 0}


def _in_window(d: date, lookback: int, lookahead: int) -> bool:
    today = date.today()
    return today - timedelta(days=lookback) <= d <= today + timedelta(days=lookahead)


def _count(db: Session) -> int:
    return db.query(TradingDate).count()
