"""A 股交易时段判定（北京时间）。

为什么单独成模块：研发计划 P0-02 要求「周末、节假日和午休不得展示为『现在可买』」，
而现有 ``/api/market/session`` 只回答了「今天是不是交易日」。仅靠 ``is_trading_day``
无法区分 09:00（开盘前）、10:30（连续竞价）、12:00（午休）、15:30（已收盘），
前端因此可能在任何时刻都显示「现在可买」。这里把时段判定做成纯函数，便于测试。

时区：中国自 1991 年起不再使用夏令时，全年 UTC+8，因此用固定偏移量而不是
``zoneinfo``（Windows 上还需额外 tzdata 依赖，属于不必要的运维成本）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

#: 北京时间（UTC+8，无夏令时）
CST = timezone(timedelta(hours=8), name="CST")

# 关键时点（北京时间）
CALL_AUCTION_START = time(9, 15)
CONTINUOUS_AM_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
CLOSE = time(15, 0)


@dataclass(frozen=True)
class Phase:
    """一个交易时段的完整描述。"""

    key: str
    label: str
    #: 此刻是否存在连续竞价（能真正成交）
    tradable: bool
    #: 此刻交易所是否接受委托
    orders_accepted: bool
    note: str


PRE_OPEN = Phase(
    key="pre_open",
    label="开盘前",
    tradable=False,
    orders_accepted=False,
    note="尚未开始集合竞价；此时显示的只能是上一交易日收盘数据。",
)
CALL_AUCTION = Phase(
    key="call_auction",
    label="集合竞价（09:15-09:25）",
    tradable=False,
    orders_accepted=True,
    note="集合竞价阶段只形成开盘参考价，09:20-09:25 不接受撤单；不构成连续成交。",
)
MORNING = Phase(
    key="morning",
    label="上午连续竞价（09:30-11:30）",
    tradable=True,
    orders_accepted=True,
    note="连续竞价进行中，行情可实时更新。",
)
NOON_BREAK = Phase(
    key="noon_break",
    label="午间休市（11:30-13:00）",
    tradable=False,
    orders_accepted=False,
    note="午休期间不接受委托、行情不更新；不得据此判断当前可买。",
)
AFTERNOON = Phase(
    key="afternoon",
    label="下午连续竞价（13:00-15:00）",
    tradable=True,
    orders_accepted=True,
    note="连续竞价进行中，行情可实时更新。",
)
CLOSED = Phase(
    key="closed",
    label="已收盘",
    tradable=False,
    orders_accepted=False,
    note="15:00 后当日行情已定；任何候选都只能作为下一交易日研究标的。",
)
NON_TRADING_DAY = Phase(
    key="non_trading_day",
    label="今日休市",
    tradable=False,
    orders_accepted=False,
    note="周末/节假日休市；最新行情为上一交易日收盘，候选只能标为下一交易日研究候选。",
)


def now_cst() -> datetime:
    """当前北京时间（带时区）。"""
    return datetime.now(CST)


def to_cst(moment: datetime | None) -> datetime | None:
    """把任意 datetime 归一到北京时间。

    naive 值按「已经是北京时间」处理（项目内 ``utc_now()`` 返回的 naive 值属例外，
    调用方需自行说明来源），避免静默猜测时区。
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CST)
    return moment.astimezone(CST)


def resolve_phase(moment: datetime | None, is_trading_day: bool) -> Phase:
    """按北京时间与「是否交易日」判定当前时段。"""
    if not is_trading_day:
        return NON_TRADING_DAY
    current = to_cst(moment or now_cst())
    clock = current.timetz().replace(tzinfo=None)
    if clock < CALL_AUCTION_START:
        return PRE_OPEN
    if clock < CONTINUOUS_AM_START:
        return CALL_AUCTION
    if clock < MORNING_END:
        return MORNING
    if clock < AFTERNOON_START:
        return NOON_BREAK
    if clock < CLOSE:
        return AFTERNOON
    return CLOSED


def candidate_label(phase: Phase, target_day, next_trading_day) -> str:
    """候选语义标签：明确「现在可买」还是「下一交易日研究候选」。"""
    if phase.tradable:
        return f"{target_day} 盘中研究候选（实时）"
    if next_trading_day is None:
        return "下一交易日研究候选（日历未覆盖下一交易日）"
    return f"下一交易日（{next_trading_day}）研究候选"
