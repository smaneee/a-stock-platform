"""统一 A 股交易规则引擎。

集中定义并计算：
- 板块分类（沪主板 / 深主板 / 创业板 / 科创板 / 北交所）
- 各板块涨跌停幅度（主板 10%、ST 5%、创业板/科创板 20%、北交所 30%）
- 新股上市初期不设涨跌停
- 买入 100 股整数手，卖出允许不足一手零股
- 最小报价单位与价格精度（Decimal）

回测引擎与模拟交易共用本引擎，禁止各自实现规则。
引擎无状态，可安全共享单例。新股上市日期由调用方提供。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from app.database.models import Security


class Board(str, Enum):
    """A 股板块分类。"""

    SH_MAIN = "sh_main"  # 上海主板 60xxxx
    SZ_MAIN = "sz_main"  # 深圳主板 00xxxx
    CHINEXT = "chinext"  # 创业板 30xxxx
    STAR = "star"  # 科创板 688xxx
    BSE = "bse"  # 北交所 8xxxxx / 4xxxxx / 92xxxx
    UNKNOWN = "unknown"  # 无法识别，按主板保守处理


# 各板块标准涨跌幅（新股上市初期不受此限，另行处理）
_BOARD_PRICE_LIMITS: dict[Board, Decimal] = {
    Board.SH_MAIN: Decimal("0.10"),
    Board.SZ_MAIN: Decimal("0.10"),
    Board.CHINEXT: Decimal("0.20"),
    Board.STAR: Decimal("0.20"),
    Board.BSE: Decimal("0.30"),
    Board.UNKNOWN: Decimal("0.10"),
}

_ST_PRICE_LIMIT = Decimal("0.05")  # ST / *ST 涨跌幅 5%

LOT_SIZE = 100  # 买入整数手（股）
TICK_SIZE = Decimal("0.01")  # 最小报价单位（元）

# 无涨跌停限制的哨兵值（新股上市初期）
NO_LIMIT = Decimal("0")

# 各板块新股无涨跌停阶段的天数（按 A 股规则）
NO_LIMIT_DAYS: dict[Board, int] = {
    Board.SH_MAIN: 1,  # 主板首日
    Board.SZ_MAIN: 1,
    Board.CHINEXT: 5,  # 创业板前 5 个交易日
    Board.STAR: 5,  # 科创板前 5 个交易日
    Board.BSE: 5,  # 北交所前 5 个交易日
    Board.UNKNOWN: 1,
}


@dataclass(frozen=True)
class MarketRules:
    """单只股票在某时点的规则快照（不可变）。"""

    board: Board
    price_limit: Decimal  # 涨跌幅比例，NO_LIMIT(0) 表示无限制
    lot_size: int
    tick_size: Decimal
    is_st: bool
    is_new_listing: bool

    @property
    def has_price_limit(self) -> bool:
        """是否有涨跌停限制。"""
        return self.price_limit != NO_LIMIT

    def limit_up(self, previous_close: Decimal) -> Decimal | None:
        """计算涨停价。无限制返回 None。昨收非法返回 None。"""
        if not self.has_price_limit:
            return None
        prev = _to_decimal(previous_close)
        if prev <= 0:
            return None
        raw = prev * (Decimal("1") + self.price_limit)
        return raw.quantize(self.tick_size, rounding=ROUND_HALF_UP)

    def limit_down(self, previous_close: Decimal) -> Decimal | None:
        """计算跌停价。无限制返回 None。昨收非法返回 None。"""
        if not self.has_price_limit:
            return None
        prev = _to_decimal(previous_close)
        if prev <= 0:
            return None
        raw = prev * (Decimal("1") - self.price_limit)
        return raw.quantize(self.tick_size, rounding=ROUND_HALF_UP)


class MarketRuleEngine:
    """统一交易规则引擎（无状态，可安全共享单例）。"""

    @staticmethod
    def classify(symbol: str) -> Board:
        """按股票代码识别板块。"""
        s = (symbol or "").strip().lower()
        if s.startswith(("sh", "sz", "bj")):
            s = s[2:]
        if len(s) != 6 or not s.isdigit():
            return Board.UNKNOWN

        if s.startswith("68"):  # 科创板 688 / 689
            return Board.STAR
        if s.startswith("6"):  # 沪主板 60xxxx
            return Board.SH_MAIN
        if s.startswith("30"):  # 创业板 30xxxx
            return Board.CHINEXT
        if s.startswith("0"):  # 深主板 00xxxx
            return Board.SZ_MAIN
        if s.startswith(("4", "8", "9")):  # 北交所 43/83/87/88/92
            return Board.BSE
        return Board.UNKNOWN

    @staticmethod
    def is_st_name(name: str) -> bool:
        """按名称判断是否 ST（含 *ST、SST、退市整理等）。"""
        return "ST" in (name or "").upper()

    def get_rules(
        self,
        symbol: str,
        name: str = "",
        is_st: bool | None = None,
        is_new_listing: bool = False,
    ) -> MarketRules:
        """计算某只股票的规则快照。

        - is_st 显式传入时以传入为准，否则按名称判断。
        - is_new_listing 为 True 时不设涨跌停（新股上市初期）。
        """
        board = self.classify(symbol)
        st = self.is_st_name(name) if is_st is None else is_st

        if st:
            price_limit = _ST_PRICE_LIMIT
        elif is_new_listing:
            price_limit = NO_LIMIT
        else:
            price_limit = _BOARD_PRICE_LIMITS[board]

        return MarketRules(
            board=board,
            price_limit=price_limit,
            lot_size=LOT_SIZE,
            tick_size=TICK_SIZE,
            is_st=st,
            is_new_listing=is_new_listing,
        )

    def get_rules_from_security(
        self,
        security: Security | None,
        current_date: date | None = None,
        fallback_name: str = "",
    ) -> MarketRules:
        """根据 SecurityMaster 中的主数据计算规则快照。

        - security 为 None 时按 fallback_name + 当前日期推导（用于回测环境无 DB 时）。
        - 自动根据 listing_date + current_date + 板块判定新股无涨跌停阶段。
        """
        symbol = security.symbol if security else ""
        name = (security.name if security else None) or fallback_name
        is_st = security.is_st if security else None
        board_str = security.board if security else self.classify(symbol).value
        listing_date = security.listing_date if security else None

        try:
            board = Board(board_str)
        except ValueError:
            board = self.classify(symbol)

        is_new_listing = False
        if listing_date is not None and current_date is not None:
            no_limit_days = NO_LIMIT_DAYS.get(board, 0)
            if no_limit_days > 0:
                delta = (current_date - listing_date).days
                is_new_listing = 0 <= delta < no_limit_days

        st = self.is_st_name(name) if is_st is None else is_st
        if st:
            price_limit = _ST_PRICE_LIMIT
        elif is_new_listing:
            price_limit = NO_LIMIT
        else:
            price_limit = _BOARD_PRICE_LIMITS[board]

        return MarketRules(
            board=board,
            price_limit=price_limit,
            lot_size=LOT_SIZE,
            tick_size=TICK_SIZE,
            is_st=st,
            is_new_listing=is_new_listing,
        )

    @staticmethod
    def validate_buy_quantity(quantity: int) -> tuple[bool, str]:
        """买入数量校验：必须为正且 100 股整数倍。"""
        if quantity <= 0:
            return False, "买入数量必须大于 0"
        if quantity % LOT_SIZE != 0:
            return False, "买入数量必须为 100 股整数手"
        return True, ""

    @staticmethod
    def validate_sell_quantity(quantity: int, available: int) -> tuple[bool, str]:
        """卖出数量校验：允许不足一手零股，但不能超过可用持仓。"""
        if quantity <= 0:
            return False, "卖出数量必须大于 0"
        if quantity > available:
            return False, "卖出数量超过可用持仓"
        return True, ""

    @staticmethod
    def round_price(price: Decimal | float) -> Decimal:
        """按最小报价单位四舍五入。"""
        return _to_decimal(price).quantize(TICK_SIZE, rounding=ROUND_HALF_UP)


def _to_decimal(value: Decimal | float | str | int) -> Decimal:
    """安全转换为 Decimal，非法值返回 0。"""
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
