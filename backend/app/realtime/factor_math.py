"""实时买点雷达用到的纯数值因子（无 IO、无第三方依赖，便于单测）。

设计约定
--------
- 数据不足时一律返回 ``None``，不抛异常、不编造数值：只有 60 根 K 线的次新股
  不应该因为算不出 60 日动量就污染排序。
- 所有函数都只看 ``values`` 的**尾部窗口**，调用方负责传入按时间升序排列的序列。
- 返回的百分比一律是**小数**（0.03 表示 3%），由上层决定展示格式。
"""
from __future__ import annotations

import math
from statistics import fmean, pstdev

# 年化波动率用的交易日数（与 A 股实际约 243~245 天取整一致）
TRADING_DAYS_PER_YEAR = 252


def _finite(*values: float) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def mean(values: list[float] | tuple[float, ...], period: int) -> float | None:
    """最后 period 个值的算术平均；不足 period 个返回 None。"""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    if not _finite(*window):
        return None
    return fmean(window)


def momentum(closes: list[float] | tuple[float, ...], period: int) -> float | None:
    """period 个交易日的累计涨幅（小数）。"""
    if period <= 0 or len(closes) < period + 1:
        return None
    base = closes[-(period + 1)]
    last = closes[-1]
    if not _finite(base, last) or base <= 0:
        return None
    return last / base - 1.0


def annualized_volatility(
    closes: list[float] | tuple[float, ...], period: int
) -> float | None:
    """日收益率标准差按 ``sqrt(252)`` 年化。"""
    if period < 2 or len(closes) < period + 1:
        return None
    window = list(closes[-(period + 1) :])
    if not _finite(*window):
        return None
    returns: list[float] = []
    for prev, cur in zip(window, window[1:]):
        if prev <= 0:
            return None
        returns.append(cur / prev - 1.0)
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(closes: list[float] | tuple[float, ...], period: int) -> float | None:
    """窗口内最大回撤（<=0 的小数，-0.2 表示最深回撤 20%）。"""
    if period < 2 or len(closes) < period:
        return None
    window = list(closes[-period:])
    if not _finite(*window):
        return None
    peak = window[0]
    worst = 0.0
    for value in window:
        if value > peak:
            peak = value
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def rolling_high(values: list[float] | tuple[float, ...], period: int) -> float | None:
    """窗口内最高值。"""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    if not _finite(*window):
        return None
    return max(window)


def rolling_low(values: list[float] | tuple[float, ...], period: int) -> float | None:
    """窗口内最低值。"""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    if not _finite(*window):
        return None
    return min(window)


def volume_ratio(volumes: list[float] | tuple[float, ...], period: int) -> float | None:
    """最新一根成交量 ÷ 之前 period 根的均值。"""
    if period <= 0 or len(volumes) < period + 1:
        return None
    base = mean(volumes[-(period + 1) : -1], period)
    if base is None or base <= 0:
        return None
    latest = volumes[-1]
    if not _finite(latest) or latest < 0:
        return None
    return latest / base


def pct_between(latest: float, base: float) -> float | None:
    """(latest / base - 1)，base 非正或非有限时返回 None。"""
    if not _finite(latest, base) or base <= 0:
        return None
    return latest / base - 1.0


def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹到 [low, high]。"""
    return max(low, min(high, value))


def boll_position(
    price: float, upper: float | None, lower: float | None
) -> float | None:
    """价格在布林带中的相对位置：0=下轨，1=上轨；带宽为 0 时返回 None。"""
    if upper is None or lower is None or not _finite(price, upper, lower):
        return None
    if upper <= lower:
        return None
    return clamp((price - lower) / (upper - lower), 0.0, 1.0)


def series_slope(values: list[float] | tuple[float, ...], period: int) -> float | None:
    """窗口首尾变化量（用于判断柱状线/均线是否在改善）。"""
    if period < 2 or len(values) < period:
        return None
    window = values[-period:]
    if not _finite(*window):
        return None
    return window[-1] - window[0]
