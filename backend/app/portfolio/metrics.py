"""组合级绩效指标。

在单标的指标（年化/最大回撤/夏普/胜率/盈亏比）基础上，补充：
换手率、集中度（赫芬达尔指数）、以及相对基准的超额收益/贝塔/阿尔法/
信息比率/跟踪误差。
"""
from __future__ import annotations

import math

import numpy as np

from app.backtest import metrics as single


def turnover(traded_values: list[float], equity_curve: list[float]) -> float:
    """换手率 = 累计成交额 / 平均资产。

    成交额只计买卖方向实际成交金额（不含费用）。平均资产为各期资产均值。
    """
    if not equity_curve:
        return 0.0
    avg_asset = float(np.mean(equity_curve))
    if avg_asset <= 0:
        return 0.0
    return float(np.sum(traded_values)) / avg_asset


def concentration(holdings_weights: list[list[float]]) -> float:
    """集中度 = 平均赫芬达尔-赫希曼指数（HHI）。

    holdings_weights 为每个交易日各标的持仓权重列表，HHI = Σ wᵢ²。
    取值范围 [1/N, 1]，越大越集中。
    """
    if not holdings_weights:
        return 0.0
    hhis = []
    for weights in holdings_weights:
        if not weights:
            continue
        hhis.append(sum(w * w for w in weights if w > 0))
    if not hhis:
        return 0.0
    return float(np.mean(hhis))


def daily_returns(equity_curve: list[float]) -> np.ndarray:
    """由资产曲线计算日收益率序列。"""
    if len(equity_curve) < 2:
        return np.array([], dtype=float)
    arr = np.array(equity_curve, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(arr) / arr[:-1]
    return np.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)


def excess_return(portfolio_return: float, benchmark_return: float) -> float:
    """超额收益 = 组合总收益 - 基准总收益。"""
    return portfolio_return - benchmark_return


def beta(portfolio_returns: np.ndarray, benchmark_returns: np.ndarray) -> float:
    """贝塔 = cov(rp, rb) / var(rb)。"""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    rp = portfolio_returns[:n]
    rb = benchmark_returns[:n]
    var_b = float(np.var(rb))
    if var_b == 0:
        return 0.0
    return float(np.cov(rp, rb)[0, 1] / var_b)


def alpha(
    portfolio_returns: np.ndarray,
    benchmark_returns: np.ndarray,
    risk_free_rate: float = 0.0,
) -> float:
    """年化阿尔法（詹森 alpha）。

    alpha = (mean(rp - rf) - beta * mean(rb - rf)) * 252
    """
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    rp = portfolio_returns[:n]
    rb = benchmark_returns[:n]
    daily_rf = risk_free_rate / 252
    b = beta(rp, rb)
    return (float(np.mean(rp)) - daily_rf - b * (float(np.mean(rb)) - daily_rf)) * 252


def tracking_error(portfolio_returns: np.ndarray, benchmark_returns: np.ndarray) -> float:
    """年化跟踪误差 = std(rp - rb) * sqrt(252)。"""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    active = portfolio_returns[:n] - benchmark_returns[:n]
    return float(np.std(active) * math.sqrt(252))


def information_ratio(
    portfolio_returns: np.ndarray,
    benchmark_returns: np.ndarray,
) -> float:
    """信息比率 = mean(active) / std(active) * sqrt(252)。"""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    active = portfolio_returns[:n] - benchmark_returns[:n]
    std = float(np.std(active))
    if std == 0:
        return 0.0
    return float(np.mean(active)) / std * math.sqrt(252)


__all__ = [
    "single",
    "turnover",
    "concentration",
    "daily_returns",
    "excess_return",
    "beta",
    "alpha",
    "tracking_error",
    "information_ratio",
]
