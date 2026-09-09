"""RSI 相对强弱指标。"""
from __future__ import annotations

import pandas as pd


def rsi(values: list[float], period: int = 14) -> list[float]:
    """计算 RSI，返回与输入等长的序列，前 period 个为 NaN。

    采用 Wilder 平滑（与常见行情软件一致）。
    """
    if period <= 0:
        raise ValueError("period 必须大于 0")
    series = pd.Series(values, dtype="float64")
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    # 当平均损失为 0 时 RSI 应为 100
    result = result.where(avg_loss != 0, 100.0)
    # 当两者都为 0 时 RSI 定义为 50（无波动）
    result = result.where((avg_gain != 0) | (avg_loss != 0), 50.0)
    return result.tolist()
