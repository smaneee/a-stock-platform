"""证券主数据服务（SecurityMasterService）。

集中存储股票名称、板块、ST 状态、上市日期等元数据。
所有需要股票基础信息的代码（回测、组合回测、模拟交易）必须经过本服务
查询，禁止依赖调用方临时传入 name/is_st/is_new_listing 等参数。

新股无涨跌停阶段规则见 app.market_rules.rules.NO_LIMIT_DAYS。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.database.models import Security
from app.market_rules.rules import Board, MarketRuleEngine, NO_LIMIT_DAYS

logger = logging.getLogger(__name__)


class SecurityMasterService:
    """证券主数据服务。每个实例绑定一个 Session。"""

    def __init__(self, db: Session):
        self._db = db

    def get(self, symbol: str) -> Security | None:
        """查询股票主数据。"""
        return self._db.get(Security, symbol)

    def ensure(
        self,
        symbol: str,
        name: str = "",
        is_st: bool | None = None,
        listing_date: date | None = None,
        source: str = "manual",
        exchange: str = "",
        delisted_date: date | None = None,
        trading_status: str | None = None,
        sector: str | None = None,
    ) -> Security:
        """按 symbol 查找或新建主数据，缺省字段由 MarketRuleEngine 推断。

        幂等性：已存在时不覆盖已有非空字段；is_st 显式传值时优先使用传入值
        （允许纠正历史错误）。
        """
        sec = self.get(symbol)
        if sec is not None:
            changed = False
            # 仅在主数据当前是 sentinel 占位符时才回填名称
            if name and (not sec.name or sec.name.startswith("未知")):
                sec.name = name
                changed = True
            if is_st is not None and sec.is_st != is_st:
                sec.is_st = is_st
                changed = True
            if listing_date is not None and sec.listing_date != listing_date:
                sec.listing_date = listing_date
                changed = True
            if delisted_date is not None and sec.delisted_date != delisted_date:
                sec.delisted_date = delisted_date
                changed = True
            if trading_status and sec.trading_status != trading_status:
                sec.trading_status = trading_status
                changed = True
            if sector and sec.sector != sector:
                sec.sector = sector
                changed = True
            if exchange and sec.exchange != exchange:
                sec.exchange = exchange
                changed = True
            if changed:
                from app.time_utils import utc_now
                sec.updated_at = utc_now()
            return sec

        board = MarketRuleEngine.classify(symbol)
        # 缺省交易所从 symbol 前缀推断
        ex = exchange or _infer_exchange(symbol)
        st = (
            MarketRuleEngine.is_st_name(name)
            if is_st is None
            else is_st
        )
        sec = Security(
            symbol=symbol,
            name=name or f"未知{symbol}",
            board=board.value,
            exchange=ex,
            is_st=st,
            listing_date=listing_date,
            delisted_date=delisted_date,
            trading_status=trading_status or "active",
            sector=sector,
            source=source,
        )
        self._db.add(sec)
        self._db.flush()
        return sec

    def is_new_listing(self, symbol: str, current_date: date | None) -> bool:
        """判断 symbol 在 current_date 是否处于新股无涨跌停阶段。

        若主数据中无 listing_date，按非新股处理（保守使用板块标准涨跌幅）。
        """
        if current_date is None:
            return False
        sec = self.get(symbol)
        if sec is None or sec.listing_date is None:
            return False
        try:
            board = Board(sec.board)
        except ValueError:
            board = Board.UNKNOWN
        no_limit_days = NO_LIMIT_DAYS.get(board, 0)
        if no_limit_days <= 0:
            return False
        delta_days = (current_date - sec.listing_date).days
        return 0 <= delta_days < no_limit_days

    def list_count(self) -> int:
        """返回主数据总数。"""
        return self._db.query(Security).count()


def _infer_exchange(symbol: str) -> str:
    """从 symbol 前缀推断交易所。

    - 6xxxxx → SH（上证主板/科创/北交所部分老股）
    - 0xxxxx / 3xxxxx → SZ（深证主板/创业板）
    - 4xxxxx / 8xxxxx → BJ（北交所）
    - 9xxxxx → BJ（北证 B 股）
    """
    if not symbol:
        return ""
    s = symbol.lstrip("0123456789") and symbol  # 兜底
    head = symbol[0]
    if head == "6":
        return "SH"
    if head in ("0", "3"):
        return "SZ"
    if head in ("4", "8", "9"):
        return "BJ"
    return ""