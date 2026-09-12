"""CCI 顺势指标。

TP  = (最高 + 最低 + 收盘) / 3
MA  = TP 的 N 日均值
MD  = TP 相对 MA 的 N 日平均绝对偏差（通达信 ``AVEDEV`` 口径）
CCI = (TP - MA) / (0.015 x MD)

MD 为 0（窗口内价格完全不动）时返回 0，避免除零；窗口不足时返回 NaN。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def cci(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """计算 CCI，返回与输入等长的序列，前 period - 1 个为 NaN。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("最高价/最低价/收盘价序列长度必须一致")

    high = pd.Series(highs, dtype="float64")
    low = pd.Series(lows, dtype="float64")
    close = pd.Series(closes, dtype="float64")
    typical = (high + low + close) / 3

    rolling = typical.rolling(window=period, min_periods=period)
    mean = rolling.mean()
    deviation = rolling.apply(
        lambda window: float(np.abs(window - window.mean()).mean()), raw=True
    )

    result = (typical - mean) / (0.015 * deviation)
    result = result.where(deviation != 0, 0.0)
    return result.tolist()
