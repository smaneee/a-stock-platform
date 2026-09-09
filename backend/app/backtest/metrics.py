"""回测绩效指标计算。"""
from __future__ import annotations

import math

import numpy as np


def total_return(final_asset: float, initial_cash: float) -> float:
    """总收益率。"""
    if initial_cash <= 0:
        return 0.0
    return (final_asset - initial_cash) / initial_cash


def annual_return(final_asset: float, initial_cash: float, trading_days: int) -> float:
    """年化收益率（按 252 个交易日折算）。"""
    if initial_cash <= 0 or trading_days <= 0:
        return 0.0
    tr = total_return(final_asset, initial_cash)
    if tr <= -1:
        return -1.0
    return (1 + tr) ** (252 / trading_days) - 1


def max_drawdown(equity_curve: list[float]) -> float:
    """最大回撤（正值百分比）。"""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def sharpe_ratio(equity_curve: list[float], risk_free_rate: float = 0.0) -> float:
    """夏普比率（基于日收益率，年化）。"""
    if len(equity_curve) < 2:
        return 0.0
    returns = np.diff(np.array(equity_curve, dtype=float)) / np.array(
        equity_curve[:-1], dtype=float
    )
    if len(returns) == 0:
        return 0.0
    std = float(np.std(returns))
    if std == 0:
        return 0.0
    daily_rf = risk_free_rate / 252
    return (float(np.mean(returns)) - daily_rf) / std * math.sqrt(252)


def win_rate(trades: list[dict]) -> float:
    """胜率 = 盈利交易数 / 总交易数。"""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return wins / len(trades)


def profit_loss_ratio(trades: list[dict]) -> float:
    """盈亏比 = 平均盈利 / 平均亏损。"""
    profits = [t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0]
    losses = [abs(t.get("pnl", 0)) for t in trades if t.get("pnl", 0) < 0]
    if not losses:
        return float("inf") if profits else 0.0
    avg_profit = sum(profits) / len(profits) if profits else 0.0
    avg_loss = sum(losses) / len(losses)
    return avg_profit / avg_loss
