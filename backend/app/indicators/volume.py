"""成交量与价格类指标：成交量均线、涨跌幅、振幅、量比。"""
from __future__ import annotations

import pandas as pd


def volume_ma(volumes: list[float], period: int = 5) -> list[float]:
    """成交量移动平均。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    series = pd.Series(volumes, dtype="float64")
    return series.rolling(window=period, min_periods=period).mean().tolist()


def change_percent(price: float, previous_close: float) -> float:
    """涨跌幅（百分比）。昨收为 0 时返回 0。"""
    if not previous_close:
        return 0.0
    return (price - previous_close) / previous_close * 100


def amplitude(high: float, low: float, previous_close: float) -> float:
    """振幅（百分比）= (最高 - 最低) / 昨收 * 100。"""
    if not previous_close:
        return 0.0
    return (high - low) / previous_close * 100


def volume_ratio(current_volume: float, past_avg_volume: float) -> float:
    """量比 = 当前成交量 / 过去平均成交量。过去均量为 0 时返回 0。"""
    if not past_avg_volume:
        return 0.0
    return current_volume / past_avg_volume


def amplitude_series(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """逐根振幅序列（%）；首根没有昨收、或任一侧数据缺失时记 NaN。"""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("最高价/最低价/收盘价序列长度必须一致")
    out: list[float] = []
    previous_close: float | None = None
    for high, low, close in zip(highs, lows, closes):
        if previous_close is None or not previous_close:
            out.append(float("nan"))
        elif high is None or low is None:
            # 缺失数据不能被当成 0 价参与运算，也不能抛异常
            out.append(float("nan"))
        else:
            out.append(amplitude(high, low, previous_close))
        previous_close = close
    return out


def volume_ratio_series(volumes: list[float], period: int = 5) -> list[float]:
    """量比序列 = 当前成交量 / **过去** period 根平均成交量。

    分母不含当前根（与 `volume_ratio(current, past_avg)` 的口径一致），因此整体
    后移一位；均量缺失或为 0、或当根成交量缺失时记 NaN，绝不返回 inf。
    """
    averages = [float("nan"), *volume_ma(volumes, period)[:-1]]
    out: list[float] = []
    for volume, average in zip(volumes, averages):
        # pandas 的 NaN 不等于自身，用它判断「窗口还没凑满」
        if volume is None or average != average or not average:
            out.append(float("nan"))
        else:
            out.append(volume / average)
    return out
