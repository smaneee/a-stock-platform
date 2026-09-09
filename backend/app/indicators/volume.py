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
