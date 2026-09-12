"""WR 威廉指标。

通达信口径：WR = (N 日最高 - 收盘) / (N 日最高 - N 日最低) x 100

默认返回 **0 ~ 100**（通达信 / 同花顺口径）：数值越小说明收盘越靠近区间高点，
越强势；越大越接近区间低点，越弱势。``signed=True`` 时减去 100，得到
TA-Lib / 海外教材常用的经典 Williams %R 口径（-100 ~ 0）。

窗口内最高等于最低（一字板 / 完全不动）时无振幅，返回中性值 50（signed 下为 -50）。
"""
from __future__ import annotations

import pandas as pd


def wr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
    *,
    signed: bool = False,
) -> list[float]:
    """计算 WR，返回与输入等长的序列，前 period - 1 个为 NaN。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("最高价/最低价/收盘价序列长度必须一致")

    high = pd.Series(highs, dtype="float64")
    low = pd.Series(lows, dtype="float64")
    close = pd.Series(closes, dtype="float64")

    highest = high.rolling(window=period, min_periods=period).max()
    lowest = low.rolling(window=period, min_periods=period).min()
    span = highest - lowest
    result = (highest - close) / span * 100
    result = result.where(span != 0, 50.0)
    if signed:
        result = result - 100.0
    return result.tolist()
