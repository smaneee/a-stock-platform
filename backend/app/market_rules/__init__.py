"""市场规则模块。

统一 A 股交易规则（板块、涨跌停、整数手、最小报价单位、T+1 交易日历），
供回测引擎与模拟交易共用，禁止分别实现。
"""
from app.market_rules.rules import (
    Board,
    MarketRuleEngine,
    MarketRules,
)
from app.market_rules.calendar import TradingCalendar

__all__ = [
    "Board",
    "MarketRuleEngine",
    "MarketRules",
    "TradingCalendar",
]
