"""ATR 真实波幅均值，以及构成它的 TR 真实波幅。

TR  = max(最高 - 最低, |最高 - 昨收|, |最低 - 昨收|)
      首根没有昨收，TR 取「最高 - 最低」。
ATR = TR 的 Wilder 平滑（等价于通达信 ``SMA(TR, N, 1)``），
      与 :mod:`app.indicators.rsi` 使用同一套平滑系数 ``alpha = 1 / N``。

ATR 量纲与价格一致（元），不是百分比；需要相对波动时请自行除以价格。
"""
from __future__ import annotations

import pandas as pd


def true_range(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[float]:
    """计算真实波幅 TR 序列（首根用「最高 - 最低」）。"""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("最高价/最低价/收盘价序列长度必须一致")
    high = pd.Series(highs, dtype="float64")
    low = pd.Series(lows, dtype="float64")
    close = pd.Series(closes, dtype="float64")
    previous_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)
    return tr.tolist()


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """计算 ATR，返回与输入等长的序列，前 period - 1 个为 NaN。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    tr = pd.Series(true_range(highs, lows, closes), dtype="float64")
    smoothed = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return smoothed.tolist()
