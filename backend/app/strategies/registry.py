"""策略注册表。

集中管理所有可用策略实例，供实时检测管道、回测引擎与 API 使用。
"""
from __future__ import annotations

from app.strategies.base import Strategy
from app.strategies.breakout import BreakoutStrategy
from app.strategies.ma_cross import MaCrossStrategy
from app.strategies.macd_cross import MacdCrossStrategy
from app.strategies.rsi_reversal import RsiReversalStrategy


def get_all_strategies() -> list[Strategy]:
    """返回所有策略实例。"""
    return [
        MaCrossStrategy(),
        BreakoutStrategy(),
        RsiReversalStrategy(),
        MacdCrossStrategy(),
    ]


def get_strategy(name: str) -> Strategy | None:
    """按名称查找策略。"""
    for strategy in get_all_strategies():
        if strategy.name == name:
            return strategy
    return None
