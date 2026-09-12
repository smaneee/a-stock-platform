"""BOLL 布林带指标。

中轨 = MA(收盘价, N)
上轨 = 中轨 + K x 标准差(收盘价, N)
下轨 = 中轨 - K x 标准差(收盘价, N)

标准差口径与通达信 ``STD`` 一致（样本标准差，``ddof=1``），而不是总体标准差。
窗口不足 period 个数据时三个序列均为 NaN。
"""
from __future__ import annotations

import pandas as pd


def boll(
    values: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """计算布林带，返回 (upper, middle, lower) 三个与输入等长的序列。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    if num_std < 0:
        raise ValueError("num_std 不能为负数")
    series = pd.Series(values, dtype="float64")
    rolling = series.rolling(window=period, min_periods=period)
    middle = rolling.mean()
    std = rolling.std(ddof=1)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper.tolist(), middle.tolist(), lower.tolist()
