"""OBV 能量潮（On Balance Volume）。

OBV[0] = 0（首根没有前收盘，不计入）
OBV[i] = OBV[i-1] + 成交量   收盘价上涨
       = OBV[i-1] - 成交量   收盘价下跌
       = OBV[i-1]            收盘价持平

与通达信 ``OBV:SUM(IF(CLOSE>REF(CLOSE,1),VOL,IF(CLOSE<REF(CLOSE,1),-VOL,0)),0)``
一致，因此首根为 0 而不是成交量。
"""
from __future__ import annotations

import pandas as pd


def obv(closes: list[float], volumes: list[float]) -> list[float]:
    """计算 OBV，返回与输入等长的累计成交量序列。

    缺失数据（None/NaN）不参与方向判断：该根输出 NaN，其后累积值继续沿用
    缺失前的状态，避免把「数据缺失」静默当成「收盘持平」。
    """
    if len(closes) != len(volumes):
        raise ValueError("收盘价与成交量序列长度必须一致")
    close = pd.Series(closes, dtype="float64")
    volume = pd.Series(volumes, dtype="float64")
    diff = close.diff()
    direction = pd.Series(0.0, index=close.index)
    direction = direction.mask(diff > 0, 1.0).mask(diff < 0, -1.0)
    increments = direction * volume
    # 首根没有昨收，约定 OBV[0] = 0（保持既有口径）；
    # 其余位置若收盘/成交量缺失或昨收缺失，则该根不参与累积，输出 NaN。
    missing = close.isna() | volume.isna()
    missing = missing.mask(close.index == 0, False)
    interior_missing = pd.Series(False, index=close.index)
    if len(close) > 1:
        interior_missing = diff.isna() & (close.index > 0) & ~close.isna()
    increments = increments.mask(missing | interior_missing, other=float("nan"))
    return increments.cumsum().tolist()
