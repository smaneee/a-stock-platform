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


# ───────────── 尾部风险（D9） ─────────────
# 研发计划要求「在账户净值上计算累计/年化收益、回撤、波动、换手和尾部风险」。
# 以下四个指标都基于**净值序列本身**（不是把多日持有收益连乘），可直接复核。


def daily_returns(equity_curve: list[float]) -> np.ndarray:
    """由净值序列得到逐日简单收益率。"""
    if len(equity_curve) < 2:
        return np.array([], dtype=float)
    values = np.array(equity_curve, dtype=float)
    previous = values[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.where(previous > 0, np.diff(values) / previous, 0.0)
    return np.asarray(returns, dtype=float)


def volatility(equity_curve: list[float], periods_per_year: int = 252) -> float:
    """年化波动率（日收益率标准差 × √252）。"""
    returns = daily_returns(equity_curve)
    if returns.size < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * math.sqrt(periods_per_year))


def value_at_risk(
    equity_curve: list[float], confidence: float = 0.95, periods_per_year: int = 252
) -> float:
    """历史模拟法 VaR（正值表示损失幅度）。

    用实际分布的分位数而不是正态假设：A 股日收益厚尾，正态近似会低估尾部风险。
    """
    returns = daily_returns(equity_curve)
    if returns.size == 0:
        return 0.0
    quantile = float(np.quantile(returns, 1 - confidence))
    return max(0.0, -quantile)


def conditional_value_at_risk(
    equity_curve: list[float], confidence: float = 0.95
) -> float:
    """CVaR / Expected Shortfall：超过 VaR 那部分损失的均值（正值表示损失）。"""
    returns = daily_returns(equity_curve)
    if returns.size == 0:
        return 0.0
    threshold = float(np.quantile(returns, 1 - confidence))
    tail = returns[returns <= threshold]
    if tail.size == 0:
        return max(0.0, -threshold)
    return max(0.0, -float(np.mean(tail)))


def max_drawdown_duration(equity_curve: list[float]) -> int:
    """最长回撤持续天数（从创新高到重新创新高的最大间隔）。"""
    if len(equity_curve) < 2:
        return 0
    peak = equity_curve[0]
    longest = 0
    current = 0
    for value in equity_curve[1:]:
        if value >= peak:
            peak = value
            longest = max(longest, current)
            current = 0
        else:
            current += 1
    return max(longest, current)


def worst_day_return(equity_curve: list[float]) -> float:
    """单日最差收益（负值）。"""
    returns = daily_returns(equity_curve)
    if returns.size == 0:
        return 0.0
    return float(np.min(returns))


#: 收益口径标注（D8）：引擎不模拟分红送转，收益是**价格收益**。
#: 该标签必须随结果一起返回，避免被误读为总收益。
RETURN_CONVENTION = "price_return_only_no_dividends"
RETURN_CONVENTION_NOTE = (
    "收益按账户净值（现金 + 持仓市值）计算，属**价格收益**：未模拟分红送转，"
    "日线采用未复权口径（除权跳空会计入收益）。与含股息的总收益不可直接比较。"
)
#: D6：前复权口径下的说明。除权跳空被还原，**不再**计入收益，
#: 因此与未复权口径的历史结果不可混比。
RETURN_CONVENTION_NOTE_QFQ = (
    "收益按账户净值（现金 + 持仓市值）计算，属**价格收益**：未模拟分红送转。"
    "日线采用前复权（qfq）口径，除权除息跳空已在本地还原、不计入收益；"
    "涨跌停判断另用未复权昨收（见 limit_reference）。与含股息的总收益仍不可直接比较。"
)

#: 复权口径中文标签（结果里一并给出，避免上游按代码猜含义）
BARS_ADJUST_LABELS = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}


def return_convention_meta(bars_adjust: str = "none") -> dict:
    """口径元信息，随每个回测结果返回。

    ``bars_adjust`` 必须是**本次实际使用的口径**（D6 起组合回测默认 qfq），
    不能写死 —— 否则结果会自称未复权而实际用了前复权。
    """
    adjust = (bars_adjust or "none").lower()
    note = RETURN_CONVENTION_NOTE if adjust == "none" else RETURN_CONVENTION_NOTE_QFQ
    return {
        "return_convention": RETURN_CONVENTION,
        "return_convention_note": note,
        "dividends_modeled": False,
        "bars_adjust": adjust,
        "bars_adjust_label": BARS_ADJUST_LABELS.get(adjust, adjust),
        # 涨跌停判定所用昨收口径：前复权收益下仍必须是未复权昨收（两者不可混用）
        "limit_reference": "unadjusted_previous_close" if adjust != "none" else "none",
        "costs_included": True,
    }
