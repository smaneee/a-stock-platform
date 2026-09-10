"""交易日历服务。

从 AKShare 同步 A 股交易日到本地数据库缓存，并提供交易日判断、下一/上一交易日查询。
T+1 解冻等逻辑必须依赖真实交易日，而非自然日或下一根 K 线。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.database.models import TradingDate

logger = logging.getLogger(__name__)

# 节假日最长连续休市约 8 天（春节），留足余量
_MAX_LOOKAHEAD_DAYS = 30


class TradingCalendar:
    """交易日历查询服务。

    每个实例绑定一个 Session，缓存已查询日期以减少重复 DB 访问。
    """

    def __init__(self, db: Session):
        self._db = db
        self._cache: dict[date, bool] = {}

    def is_trading_day(self, d: date) -> bool:
        """判断某日是否为交易日。"""
        if d in self._cache:
            return self._cache[d]
        row = self._db.get(TradingDate, d)
        result = row is not None
        self._cache[d] = result
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


def sync_trading_calendar(db: Session) -> int:
    """从 AKShare 同步交易日历到本地数据库，返回新增条数。

    AKShare 不可用或网络失败时返回 0，不抛异常（调用方据返回值提示）。
    """
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        logger.warning("AKShare 未安装，无法同步交易日历")
        return 0

    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # noqa: BLE001
        logger.warning("AKShare 同步交易日历失败: %s", exc)
        return 0

    if df is None or df.empty:
        return 0

    added = 0
    for raw in df["trade_date"].tolist():
        try:
            d = raw.date() if hasattr(raw, "date") else date.fromisoformat(str(raw))
        except (ValueError, TypeError):
            continue
        if db.get(TradingDate, d) is None:
            db.add(TradingDate(trade_date=d))
            added += 1

    db.commit()
    logger.info("交易日历同步完成，新增 %d 条", added)
    return added
