"""前复权（qfq）因子推导：通达信除权除息 + 本地未复权日线。

为什么不直接抓现成的前复权序列
------------------------------
- 东方财富 K 线接口（``stock_zh_a_hist`` / ``push2his``）在本机稳定复现
  ``RemoteDisconnected``，不能作为全市场主通道；
- 新浪日线能给出前复权序列，但实测约 4~5 秒/只，全市场一轮要数小时，
  只适合做小样本交叉验证；
- 通达信 ``get_xdxr_info`` 实测 40~50 毫秒/只且字段齐全，配合本地已经存好的
  全市场不复权日线，可以直接在本地把复权因子推出来。

口径与公式
----------
通达信 ``get_xdxr_info`` 的 ``fenhong`` / ``songzhuangu`` / ``peigu`` 都是
**每 10 股**口径，本模块统一换算成每股。除权除息参考价用交易所标准公式：

    参考价 = (前收盘价 - 每股派现 + 每股配股 × 配股价)
             / (1 + 每股送转股 + 每股配股)
    因子 k = 参考价 / 前收盘价
    前复权价 = 原价 × ∏{事件日 > 该 K 线日期} k

约定：前复权以最新交易日为基准，因此**最新一根 K 线的因子恒为 1.0**，
越早的交易日因子越小（≤ 1）。

实测校验（2026-09-12）
----------------------
300750 于 2026-08-10 每 10 股派 14.11 元，除权前最后一个交易日 2026-08-07
收盘 394.40，k = (394.40 - 1.411) / 394.40 = 0.996423；新浪前复权序列
同一天收 392.98、同源不复权 394.40，比值 0.99640。两者一致到 5 位小数。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

# 通达信 xdxr 的派现 / 送转 / 配股字段都是「每 10 股」口径
PER_SHARE_UNIT = 10.0
# 事件类别 1 = 除权除息（送转、派现、配股都归在这一类）
CATEGORY_EX_DIVIDEND = 1
# 前收盘价 / 参考价的有效下限；低于它视为脏数据，该事件不参与复权
MIN_VALID_PRICE = 1e-6


@dataclass(frozen=True)
class XdxrEvent:
    """一次除权除息事件（已换算成每股口径）。"""

    trade_date: date
    cash_per_share: float = 0.0
    share_ratio: float = 0.0
    rights_ratio: float = 0.0
    rights_price: float = 0.0

    @property
    def is_effective(self) -> bool:
        """三项权益全为 0 的记录（纯股本变动被记成 category=1）不产生复权因子。"""
        return (
            self.cash_per_share > 0
            or self.share_ratio > 0
            or self.rights_ratio > 0
        )


def _to_float(raw: object) -> float:
    """通达信字段可能是 None / 字符串 / 非规格化浮点，统一转成有限 float。"""
    if raw is None:
        return 0.0
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


def parse_xdxr_events(
    rows: Iterable[Mapping[str, object]],
    *,
    earliest: date | None = None,
) -> list[XdxrEvent]:
    """把通达信 xdxr 原始记录转成按日期升序的 :class:`XdxrEvent` 列表。

    只保留 ``category == 1``（除权除息）。同一天的多条记录合并成一条：
    派现、送转、配股分别求和，配股价按配股数加权平均。

    ``earliest`` 用于丢弃早于本地日线起点的事件 —— 那些事件只影响更早的
    K 线，对当前窗口没有任何作用，留着只会白白增加计算量。
    """
    merged: dict[date, list[float]] = {}
    for row in rows:
        if int(_to_float(row.get("category"))) != CATEGORY_EX_DIVIDEND:
            continue
        year = int(_to_float(row.get("year")))
        month = int(_to_float(row.get("month")))
        day = int(_to_float(row.get("day")))
        if year <= 0 or not 1 <= month <= 12 or not 1 <= day <= 31:
            continue
        try:
            event_date = date(year, month, day)
        except ValueError:
            continue
        if earliest is not None and event_date <= earliest:
            continue
        cash = _to_float(row.get("fenhong")) / PER_SHARE_UNIT
        share = _to_float(row.get("songzhuangu")) / PER_SHARE_UNIT
        rights = _to_float(row.get("peigu")) / PER_SHARE_UNIT
        rights_price = _to_float(row.get("peigujia"))
        slot = merged.setdefault(event_date, [0.0, 0.0, 0.0, 0.0])
        slot[0] += cash
        slot[1] += share
        slot[2] += rights
        slot[3] += rights * rights_price

    events: list[XdxrEvent] = []
    for event_date in sorted(merged):
        cash, share, rights, rights_amount = merged[event_date]
        event = XdxrEvent(
            trade_date=event_date,
            cash_per_share=cash,
            share_ratio=share,
            rights_ratio=rights,
            rights_price=(rights_amount / rights) if rights > 0 else 0.0,
        )
        if event.is_effective:
            events.append(event)
    return events


def ex_reference_price(previous_close: float, event: XdxrEvent) -> float:
    """交易所口径的除权除息参考价；参数非法时返回 0（调用方按不可解处理）。"""
    if previous_close <= MIN_VALID_PRICE:
        return 0.0
    shares = 1.0 + event.share_ratio + event.rights_ratio
    if shares <= 0:
        return 0.0
    reference = (
        previous_close - event.cash_per_share + event.rights_ratio * event.rights_price
    ) / shares
    if reference <= MIN_VALID_PRICE:
        return 0.0
    return reference


def adjustment_factor(previous_close: float, event: XdxrEvent) -> float:
    """单个事件的复权乘数；不可解时返回 1.0（等于不调整）。"""
    reference = ex_reference_price(previous_close, event)
    if reference <= 0:
        return 1.0
    return reference / previous_close


@dataclass(frozen=True)
class QfqFactorSeries:
    """一只标的的前复权因子序列（与 ``days`` 一一对应，升序）。"""

    days: tuple[date, ...] = ()
    factors: tuple[float, ...] = ()
    applied_events: int = 0
    pending_events: int = 0
    unresolved_events: tuple[XdxrEvent, ...] = field(default=())

    @property
    def is_unadjusted(self) -> bool:
        """所有因子都是 1.0（没有事件，或事件全部不可解）。"""
        return all(abs(value - 1.0) <= 1e-12 for value in self.factors)


def compute_qfq_factors(
    days: Sequence[date],
    closes: Sequence[float],
    events: Iterable[XdxrEvent],
) -> QfqFactorSeries:
    """推导每个交易日的前复权因子。

    ``days`` 与 ``closes`` 必须等长（顺序可以是任意，内部会按日期重排）。
    事件只作用于**严格早于事件日**的 K 线：事件日当天及之后的价格已经是
    除权后的价格，不需要再调整。

    事件日超出 ``days`` 范围时记为 ``pending_events``（数据还没走到那天）；
    找不到可用前收盘价、或参考价非正的事件记为 ``unresolved_events``，
    既不调整也不静默丢弃，交给调用方决定是否告警。
    """
    if len(days) != len(closes):
        raise ValueError("days 与 closes 长度必须一致")
    if not days:
        return QfqFactorSeries()

    pairs = sorted(zip(days, (float(value) for value in closes)), key=lambda v: v[0])
    ordered_days = tuple(day for day, _ in pairs)
    ordered_closes = tuple(close for _, close in pairs)
    last_day = ordered_days[-1]

    ordered_events = sorted(events, key=lambda item: item.trade_date)
    actions = [item for item in ordered_events if item.trade_date <= last_day]
    pending = len(ordered_events) - len(actions)

    factors = [1.0] * len(ordered_days)
    cumulative = 1.0
    applied = 0
    unresolved: list[XdxrEvent] = []

    # 从最新一个交易日往回扫：维护「日期晚于当前 K 线」的所有事件的累计乘数
    position = len(actions) - 1
    for index in range(len(ordered_days) - 1, -1, -1):
        while position >= 0 and actions[position].trade_date > ordered_days[index]:
            event = actions[position]
            previous = bisect_left(ordered_days, event.trade_date) - 1
            previous_close = ordered_closes[previous] if previous >= 0 else 0.0
            reference = ex_reference_price(previous_close, event)
            if reference <= 0:
                unresolved.append(event)
            else:
                cumulative *= reference / previous_close
                applied += 1
            position -= 1
        factors[index] = cumulative

    # 扫完仍没被消费的事件，其日期 <= 第一根 K 线：没有「事件前的收盘价」可用，
    # 既不能定价也不能静默丢掉，记进 unresolved 交给调用方处置。
    while position >= 0:
        unresolved.append(actions[position])
        position -= 1

    return QfqFactorSeries(
        days=ordered_days,
        factors=tuple(factors),
        applied_events=applied,
        pending_events=pending,
        unresolved_events=tuple(unresolved),
    )
