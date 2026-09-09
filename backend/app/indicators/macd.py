"""MACD 指标。

MACD = EMA(fast) - EMA(slow)
信号线 DEA = MACD 的 EMA(signal)
柱状图 = (MACD - DEA) * 2
"""
from __future__ import annotations

import pandas as pd

from app.indicators.moving_average import ema


def macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """计算 MACD，返回 (dif, dea, hist) 三个等长序列。"""
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("周期必须大于 0")
    if fast >= slow:
        raise ValueError("fast 周期必须小于 slow 周期")

    ema_fast = pd.Series(ema(values, fast), dtype="float64")
    ema_slow = pd.Series(ema(values, slow), dtype="float64")
    dif = (ema_fast - ema_slow).tolist()

    # DEA 为 dif 的 EMA，去除前段 NaN 后再计算
    dif_series = pd.Series(dif, dtype="float64")
    dea = dif_series.ewm(span=signal, adjust=False, min_periods=signal).mean().tolist()

    dif_arr = pd.Series(dif, dtype="float64")
    dea_arr = pd.Series(dea, dtype="float64")
    hist = ((dif_arr - dea_arr) * 2).tolist()
    return dif, dea, hist
