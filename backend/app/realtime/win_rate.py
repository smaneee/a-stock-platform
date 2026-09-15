"""「赚钱率」= 同类条件的历史回放胜率（**样本内**，不是未来上涨概率）。

## 这个数字是什么、不是什么

* **是什么**：对当前排名靠前的标的，把"今天满足的条件条数"当作门槛，在**本地历史日线**
  上逐日回放（每天都只用当天及以前的数据，杜绝未来函数），统计此后持有 ``hold_days``
  个交易日的收益，给出**胜率 / 平均收益 / 样本数**。数据全部来自本地 ``historical_bars``
  （前复权），每次调用都重新计算，不缓存结论。
* **不是什么**：它**不是**未来上涨概率，**不是**经过样本外验证的策略胜率，
  也**不扣交易费用**。平台策略证据页的 ``production_ready_count`` 至今为 0，
  所以这个数字只能用来**排序研究优先级**，不能当作收益承诺。

## 回放口径（必须如实告知）

条件共 8 条（见 ``app/realtime/screener.py``），其中 5 条只需要日线序列即可复算，
回放时只用这 5 条：站上 20 日线、20 日线在 60 日线上方、20 日动量为正、
RSI 落在健康区、成交量温和放大。剩下 3 条（MACD 柱转强、布林位置回踩、波动率可控）
依赖更细的口径，历史回放未纳入 —— 因此回放规则与实时选股规则**不完全相同**，
这一点写在返回值里（``replay_note``）。

买卖时点：信号出现在第 t 日收盘 → 第 t+1 日收盘买入 → 第 t+1+hold 日收盘卖出，
收益 = close[t+1+hold] / close[t+1] − 1。两端数据不足的样本直接丢弃，不插值。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.indicators.moving_average import ma
from app.indicators.rsi import rsi

#: 回放使用的条件（与实时选股 8 条中的可复算子集）
REPLAY_CONDITIONS = (
    "above_ma20",
    "ma20_above_ma60",
    "momentum_positive",
    "rsi_healthy",
    "volume_expand",
)
#: 回放门槛：命中条数 ≥ 该值即视为"同类信号"
MIN_HITS_DEFAULT = 4
#: 成交量放大阈值（与 screener.VOLUME_EXPAND_MIN 同口径）
VOLUME_EXPAND_MIN = 1.2
#: 复算需要的最小窗口
MIN_WINDOW = 61


def _last(series: list[float], index: int) -> float | None:
    """取 ``index`` 处（含）之前最后一个有效值；不足返回 None。"""
    for i in range(index, -1, -1):
        value = series[i]
        if value is not None and value == value:  # 非 NaN
            return float(value)
    return None


def _hits_at(
    index: int,
    closes: list[float],
    volumes: list[float],
    ma20: list[float],
    ma60: list[float],
    rsi14: list[float],
    volume_ma20: list[float],
) -> tuple[list[str], int]:
    """第 ``index`` 日满足哪些条件（只用 ≤ index 的数据）。"""
    price = closes[index]
    hits: list[str] = []
    m20 = _last(ma20, index)
    m60 = _last(ma60, index)
    if m20 is not None and price >= m20:
        hits.append("above_ma20")
    if m20 is not None and m60 is not None and m20 >= m60:
        hits.append("ma20_above_ma60")
    if index >= 20 and closes[index - 20] > 0 and price > closes[index - 20]:
        hits.append("momentum_positive")
    rsi_value = _last(rsi14, index)
    if rsi_value is not None and 35.0 <= rsi_value <= 65.0:
        hits.append("rsi_healthy")
    vol_ma = _last(volume_ma20, index)
    if vol_ma and vol_ma > 0 and volumes[index] / vol_ma >= VOLUME_EXPAND_MIN:
        hits.append("volume_expand")
    return hits, len(hits)


@dataclass(frozen=True)
class ReplayStats:
    samples: int
    wins: int
    win_rate: float | None
    mean_return: float | None
    median_return: float | None
    best: float | None
    worst: float | None
    hold_days: int
    min_hits: int
    conditions_used: tuple[str, ...]
    note: str

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "best": self.best,
            "worst": self.worst,
            "hold_days": self.hold_days,
            "min_hits": self.min_hits,
            "conditions_used": list(self.conditions_used),
            "note": self.note,
        }


EMPTY_NOTE = (
    "样本不足：历史回放里没有出现「同类条件」（命中条数达不到门槛），"
    "或可用日线长度不够；此时不给出胜率，也不用其他标的的数字代替"
)


def replay_signal(
    closes: list[float],
    volumes: list[float],
    *,
    hold_days: int = 5,
    min_hits: int = MIN_HITS_DEFAULT,
    warmup: int = MIN_WINDOW,
) -> ReplayStats:
    """在历史序列上回放"同类条件"，返回样本内统计。

    * ``closes`` / ``volumes`` 必须**升序**且等长；
    * 每天只用当天及以前的数据计算指标（forward return 只用之后的价格）；
    * 样本两端不足（缺 warmup 或未来 ``hold_days+1`` 根）时直接丢弃。
    """
    length = len(closes)
    if length < warmup + hold_days + 2:
        return ReplayStats(0, 0, None, None, None, None, None, hold_days, min_hits,
                           REPLAY_CONDITIONS, EMPTY_NOTE)

    ma20 = ma(closes, 20)
    ma60 = ma(closes, 60)
    rsi14 = rsi(closes, 14)
    volume_ma20 = ma(volumes, 20)

    returns: list[float] = []
    # 最后一个可评估的信号日：买入日 +1、卖出日 +hold，都必须存在
    last_signal = length - hold_days - 2
    for index in range(warmup, last_signal + 1):
        _, hits = _hits_at(index, closes, volumes, ma20, ma60, rsi14, volume_ma20)
        if hits < min_hits:
            continue
        buy = closes[index + 1]
        sell = closes[index + 1 + hold_days]
        if buy <= 0 or sell <= 0:
            continue
        returns.append(sell / buy - 1.0)

    if not returns:
        return ReplayStats(0, 0, None, None, None, None, None, hold_days, min_hits,
                           REPLAY_CONDITIONS, EMPTY_NOTE)

    wins = sum(1 for value in returns if value > 0)
    ordered = sorted(returns)
    mid = len(ordered) // 2
    median = (
        ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    return ReplayStats(
        samples=len(returns),
        wins=wins,
        win_rate=round(wins / len(returns), 4),
        mean_return=round(sum(returns) / len(returns), 4),
        median_return=round(median, 4),
        best=round(ordered[-1], 4),
        worst=round(ordered[0], 4),
        hold_days=hold_days,
        min_hits=min_hits,
        conditions_used=REPLAY_CONDITIONS,
        note=(
            f"样本内回放：{len(returns)} 次同类条件，持有 {hold_days} 个交易日（T+1 收盘买入），"
            "未扣交易费用；只用 5 条可由日线复算的条件，与实时选股的 8 条不完全相同。"
            "**不是未来上涨概率，也没有通过样本外验证**"
        ),
    )
