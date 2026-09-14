"""涨跌停判断的未复权昨收（P0-05 的 D6）。

研发计划 §7.4 / §5.2：

> 除权日：未复权涨跌停判断与前复权策略收益口径各自正确，且不可互用。

现状与问题：组合回测原本整条链路都用 ``adjust="none"``（未复权）日线。这样涨跌停
判断是对的，但**策略输入与收益口径错了** —— 除权除息的跳空会被当成真实亏损，
库内已有的 660 万行前复权日线完全没被回测使用。

修复口径（本模块）：

* **策略 / 指标 / 账户净值** 用前复权（``adjust="qfq"``）；
* **涨跌停判断** 用未复权序列的「上一交易日收盘价」；
* 两者按日期对齐合并：前复权 bar 携带未复权昨收，执行层据此算板价。

不做的事：不猜测缺失日期。对齐不上时 ``previous_close`` 保持 0，执行层会据此跳过
涨跌停约束（并在结果里标注），而不是用复权价冒充。
"""
from __future__ import annotations

from datetime import date
from typing import Sequence

from app.market_data.base import QuoteData


def _bar_date(bar: QuoteData) -> date:
    stamp = bar.market_time or bar.received_at
    return stamp.date()


def unadjusted_previous_close_map(
    unadjusted_bars: Sequence[QuoteData],
) -> dict[date, float]:
    """未复权序列 → {交易日: 上一交易日收盘价}。"""
    ordered = sorted(unadjusted_bars, key=_bar_date)
    mapping: dict[date, float] = {}
    previous_close = 0.0
    for bar in ordered:
        mapping[_bar_date(bar)] = previous_close
        previous_close = float(bar.price or 0.0)
    return mapping


def attach_unadjusted_previous_close(
    bars: Sequence[QuoteData],
    unadjusted_bars: Sequence[QuoteData],
) -> tuple[list[QuoteData], int]:
    """把未复权昨收写到（前复权）bar 上，返回 ``(bars, 未能对齐的根数)``。

    * 对齐得上：``previous_close`` = 同交易日的未复权昨收；
    * 对齐不上（未复权序列缺该日期，或该日无更早数据）：保持 0 并计入 ``missing``，
      调用方应据此在结果里标注「部分标的未参与涨跌停约束」。
    """
    mapping = unadjusted_previous_close_map(unadjusted_bars)
    enriched: list[QuoteData] = []
    missing = 0
    for bar in bars:
        day = _bar_date(bar)
        if day not in mapping:
            # 未复权序列里没有这个交易日：无法还原昨收，保持 0 并计入缺失
            missing += 1
        enriched.append(bar.model_copy(update={"previous_close": float(mapping.get(day, 0.0))}))
    return enriched, missing
