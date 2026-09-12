"""KDJ 随机指标。

RSV = (收盘 - N 日最低) / (N 日最高 - N 日最低) x 100
K   = SMA(RSV, M1, 1) = (K_prev x (M1 - 1) + RSV) / M1
D   = SMA(K,  M2, 1) = (D_prev x (M2 - 1) + K) / M2
J   = 3K - 2D

SMA(X, N, 1) 是通达信口径的平滑均值，等价于 ``alpha = 1 / N`` 的指数平滑，
首个有效值直接取当根 RSV。

边界约定：
- 窗口不足 period 根时最高/最低无定义，RSV 与 K/D/J 均为 NaN。
- 窗口内最高等于最低（一字板 / 完全不动）时无振幅，RSV 约定为 50（中性）。
- J 值允许越出 0~100 区间，这是 KDJ 的正常表现。
"""
from __future__ import annotations

import pandas as pd


def kdj(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 9,
    k_period: int = 3,
    d_period: int = 3,
) -> tuple[list[float], list[float], list[float]]:
    """计算 KDJ，返回 (k, d, j) 三个与输入等长的序列。"""
    if period <= 0 or k_period <= 0 or d_period <= 0:
        raise ValueError("周期必须大于 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("最高价/最低价/收盘价序列长度必须一致")

    high = pd.Series(highs, dtype="float64")
    low = pd.Series(lows, dtype="float64")
    close = pd.Series(closes, dtype="float64")

    highest = high.rolling(window=period, min_periods=period).max()
    lowest = low.rolling(window=period, min_periods=period).min()
    span = highest - lowest
    rsv = (close - lowest) / span * 100
    # 无振幅时 RSV 约定为 50；窗口不足或输入缺失处保持 NaN
    rsv = rsv.where(span != 0, 50.0)
    rsv = rsv.where(highest.notna() & lowest.notna() & close.notna())

    k = rsv.ewm(alpha=1 / k_period, adjust=False).mean().where(rsv.notna())
    d = k.ewm(alpha=1 / d_period, adjust=False).mean().where(rsv.notna())
    j = 3 * k - 2 * d
    return k.tolist(), d.tolist(), j.tolist()
