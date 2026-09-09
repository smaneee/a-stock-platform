"""移动平均指标：MA（简单移动平均）与 EMA（指数移动平均）。

所有函数输入价格序列，返回与输入等长的序列，数据不足处为 NaN。
"""
from __future__ import annotations

import pandas as pd


def ma(values: list[float], period: int) -> list[float]:
    """简单移动平均 MA。数据不足 period 时为 NaN。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=period, min_periods=period).mean().tolist()


def ema(values: list[float], period: int) -> list[float]:
    """指数移动平均 EMA。前 period-1 个值为 NaN。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    series = pd.Series(values, dtype="float64")
    return series.ewm(span=period, adjust=False, min_periods=period).mean().tolist()


def latest_valid(series: list[float]) -> float | None:
    """返回序列中最后一个非 NaN 值，全 NaN 时返回 None。"""
    for value in reversed(series):
        if value is not None and value == value:  # 排除 NaN
            return value
    return None
