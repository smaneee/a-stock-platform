"""实时买点雷达：从全市场实时行情里挑出「现在可以考虑买入」的标的。

**研究工具，不构成投资建议。** 全部计算只用 ``<= as_of`` 的数据，不含未来函数。

实测口径（2026-09-12 本机，非推断）
----------------------------------
- 行情：全池 5550 只。单条通达信连接 4.93s；4~5 条独立连接并行 1.34~2.22s，
  五条连接全部返回 5549/5549（仅 688801 上游无数据）。
- 日线：本地 ``historical_bars`` 最近 120 个交易日；**该次测量时缓存只有不复权
  （``adjust=none``）**，SQL 窗口查询 2.6s，按 (快照, 最后交易日, 行数) 缓存后
  不再重复读取。⚠ 现行代码已改为**优先前复权**（见下方 ``resolve_bar_adjust``：
  优先 ``qfq``、缺失或落后时回退 ``none``），因此上句的 2.6s 是当时的
  ``none`` 口径测量值，不是当前 qfq 口径的基准。
- 收盘后（含周末）通达信行情与「最近一个交易日」的日线**逐字段完全一致**：
  已核对 600000 / 000001 / 300750 / 688981 / 920819 / 601398 / 002594 / 600519
  的 price / open / high / low / volume / amount 全部相等。所以**不能**把行情再
  拼一根当日 K 线，否则同一交易日的量价会被重复计入两次。
- 行情 ``vol`` 已由数据源层 ×100 转成「股」，``amount`` 单位是「元」，与
  ``historical_bars`` 的列口径一致。
- 行情 ``market_time`` 是通达信**服务器时间**（休市时实测 2026-09-12T15:30），
  不是成交时间，因此判断「行情属于哪一天」绝不能依赖它。

当日 K 线的合并规则（不依赖时钟，可自证）
----------------------------------------
逐只比较行情与最后一根日线，只有当 volume / amount / close **与最后一根日线不
完全一致**时，才认为行情带了新一天的数据：最后一根日线已经是当天就替换它，
否则追加一根（OHLCV 全部取自行情）。完全一致 → 序列已经含当天，原样使用。
开盘前 / 盘中 / 收盘后 / 周末都不会重复计入或漏计当日。

指标口径
--------
RSI / ATR / KDJ 都是 Wilder 指数平滑，数值会受起点影响，因此窗口不能随便砍。
实测（500 只随机标的，与本地全量 242 根对比）最大偏差：

===========  ==============  ====================  ========
起点根数      RSI14            ATR14（相对）          KDJ-K
===========  ==============  ====================  ========
40           16.55           15.83%                0
60            6.05            4.45%                0
90            0.72            0.42%                0
120           0.086           0.067%               0
180           0.001           0.0004%              0
===========  ==============  ====================  ========

120 根已经和全量几乎无差（RSI 差 <0.1、ATR 差 <0.1%），所以扫描统一用 120 根
窗口，既快又与「技术指标」页的口径一致。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Awaitable, Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import (
    HistoricalBar,
    LimitUpSentiment,
    UniverseMember,
    UniverseSnapshot,
)
from app.database.session import SessionLocal
from app.indicators.atr import atr
from app.indicators.boll import boll
from app.indicators.kdj import kdj
from app.indicators.macd import macd
from app.indicators.moving_average import latest_valid
from app.indicators.rsi import rsi
from app.market_data.base import QuoteData
from app.market_data.provider_manager import ProviderManager
from app.market_data.tdx_provider import TdxProvider
from app.market_rules.calendar import TradingCalendar
from app.market_rules.rules import MarketRuleEngine
from app.portfolio.sentiment import (
    SentimentGate,
    SentimentGateConfig,
    SentimentSnapshot,
)
from app.realtime import factor_math as fm
from app.market_rules.session_state import now_cst
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 120

#: 复权口径/缓存键解析结果的 TTL（秒）。实测盘中每次扫描该聚合约 6 秒，
#: 日线在盘中不会被本进程改写，60 秒 TTL 足以摊销又不至于长期用错口径。
ADJUST_CACHE_TTL_SECONDS = 60.0
MIN_BARS_FOR_FACTORS = 61
# 行情与日线「是否为同一份数据」的相对容差
SAME_VALUE_REL_TOLERANCE = 1e-6
# 成交量单位换算容差：通达信行情是「手 × 100」，本地日线来自其它源，
# 同一根 K 线允许差 1 手（实测 2026-09-11 有 32 只标的恰好差 100 股）
VOLUME_SAME_ABS_TOLERANCE = 100.0

BAR_PERIOD = "daily"
# 存量日线的复权口径（前复权回填之前，库里只有不复权）
BAR_ADJUST = "none"
# 优先使用的复权口径。前复权能消掉除权除息造成的假跳空，比不复权更接近真实
# 收益；前复权数据缺失、或回填进度落后于不复权时自动回退，见 resolve_bar_adjust。
PREFERRED_BAR_ADJUST = "qfq"
FALLBACK_BAR_ADJUST = BAR_ADJUST
# 复权口径的中文标签（写进 notes 与前端展示）
ADJUST_LABELS: dict[str, str] = {
    "none": "不复权",
    "qfq": "前复权",
    "hfq": "后复权",
}

QuoteFetcher = Callable[[Sequence[str]], Awaitable[dict[str, QuoteData]]]


def _same_value(left: float, right: float) -> bool:
    """按相对容差比较两个浮点量，用于判断行情与日线是否同一份数据。"""
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= SAME_VALUE_REL_TOLERANCE * scale


def _same_volume(left: float, right: float) -> bool:
    """成交量同一性：允许 1 手的单位换算差（见 ``VOLUME_SAME_ABS_TOLERANCE``）。"""
    return (
        abs(left - right) <= VOLUME_SAME_ABS_TOLERANCE or _same_value(left, right)
    )


def resolve_bar_adjust(
    db: Session, period: str = BAR_PERIOD
) -> tuple[str, int, date | None]:
    """选择实际使用的复权口径，返回 ``(adjust, max_id, last_day)``。

    优先前复权；前复权数据缺失、或者最后交易日比不复权更早（回填落后于行情
    入库）时回退到不复权，避免拿更短的历史去算因子。两个口径一次查询搞定，
    ``max_id`` 与 ``last_day`` 直接复用为缓存失效判断的键。
    """
    rows = db.execute(
        select(
            HistoricalBar.adjust,
            func.max(HistoricalBar.id),
            func.max(HistoricalBar.trade_date),
        )
        .where(
            HistoricalBar.period == period,
            HistoricalBar.adjust.in_((PREFERRED_BAR_ADJUST, FALLBACK_BAR_ADJUST)),
        )
        .group_by(HistoricalBar.adjust)
    ).all()
    stats: dict[str, tuple[int, date | None]] = {
        row[0]: (int(row[1] or 0), row[2]) for row in rows
    }
    preferred = stats.get(PREFERRED_BAR_ADJUST)
    fallback = stats.get(FALLBACK_BAR_ADJUST)
    preferred_day = preferred[1] if preferred else None
    fallback_day = fallback[1] if fallback else None
    if preferred is not None and preferred_day is not None:
        if fallback_day is None or preferred_day >= fallback_day:
            return PREFERRED_BAR_ADJUST, preferred[0], preferred_day
    if fallback is not None and fallback_day is not None:
        return FALLBACK_BAR_ADJUST, fallback[0], fallback_day
    if preferred is not None:
        return PREFERRED_BAR_ADJUST, preferred[0], preferred_day
    return FALLBACK_BAR_ADJUST, 0, None


#: 复权口径解析的进程级短 TTL 缓存：(单调时刻, adjust, max_id, last_day)。
#: 实测动机（2026-09-14，主库 13,359,225 行日线）：`resolve_bar_adjust` 的
#: `GROUP BY adjust` 聚合 **p50 ≈ 6.06 秒**（同库里 `MAX(id)` 与按主键取一行都是 0.0ms），
#: 因为 `historical_bars` 只有 `symbol` 与 `trade_date` 两个索引，没有 `(period, adjust)`。
#: 指标接口每次请求都要先算这个口径 → 单次 7.7 秒；改成共享缓存后只有 60 秒一次。
_ADJUST_CACHE: tuple[float, str, int, date | None] | None = None


def resolve_bar_adjust_cached(
    db: Session, period: str = BAR_PERIOD
) -> tuple[str, int, date | None]:
    """带 TTL 的 `resolve_bar_adjust`，**进程内共享**（扫描 / 指标 / 验证共用一份）。

    为什么要共享而不是各留一份：这个查询是纯读且按分钟才可能变化，重复执行纯属浪费；
    指标接口此前每次请求都付 6 秒，就是因为只有扫描服务自己有缓存。

    测试注意：缓存跨用例存活会造成口径串味，因此 `backend/tests/conftest.py` 里有
    autouse fixture 每个用例前调用 :func:`reset_adjust_cache`。
    """
    global _ADJUST_CACHE
    now = time.monotonic()
    cached = _ADJUST_CACHE
    if cached is not None and now - cached[0] < ADJUST_CACHE_TTL_SECONDS:
        return cached[1], cached[2], cached[3]
    adjust, max_id, last_day = resolve_bar_adjust(db, period)
    _ADJUST_CACHE = (now, adjust, max_id, last_day)
    return adjust, max_id, last_day


def reset_adjust_cache() -> None:
    """清空口径缓存（测试隔离与「刚回填完前复权需要立刻生效」时使用）。"""
    global _ADJUST_CACHE
    _ADJUST_CACHE = None


@dataclass(frozen=True)
class ScreenerConfig:
    """扫描参数；全部有默认值，接口可覆盖。"""

    top_n: int = 10
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    refine_pool: int = 200
    exclude_st: bool = True
    min_amount_20: float = 50_000_000.0
    min_price: float = 2.0
    max_price: float = 1000.0
    max_change_pct: float = 7.0
    min_change_pct: float = -7.0
    min_triggers: int = 3
    stop_atr_multiple: float = 2.0
    target_atr_multiple: float = 3.0
    risk_budget_pct: float = 1.0
    max_weight_pct: float = 20.0
    sentiment_lag_days: int = 1
    scale_exposure_by_sentiment: bool = True

    def validate(self) -> None:
        if not 1 <= self.top_n <= 100:
            raise ValueError("top_n 必须在 1..100")
        if not MIN_BARS_FOR_FACTORS <= self.lookback_days <= 800:
            raise ValueError(f"lookback_days 必须在 {MIN_BARS_FOR_FACTORS}..800")
        if not self.top_n <= self.refine_pool <= 3000:
            raise ValueError("refine_pool 必须在 top_n..3000")
        if self.min_amount_20 < 0:
            raise ValueError("min_amount_20 不能为负")
        if self.min_price <= 0 or self.max_price <= self.min_price:
            raise ValueError("价格区间非法")
        if self.min_change_pct >= self.max_change_pct:
            raise ValueError("涨跌幅区间非法")
        if not 0 <= self.min_triggers <= 8:
            raise ValueError("min_triggers 必须在 0..8")
        if self.stop_atr_multiple <= 0 or self.target_atr_multiple <= 0:
            raise ValueError("ATR 倍数必须大于 0")
        if not 0 < self.risk_budget_pct <= 100:
            raise ValueError("risk_budget_pct 必须在 (0, 100]")
        if not 0 < self.max_weight_pct <= 100:
            raise ValueError("max_weight_pct 必须在 (0, 100]")
        if self.sentiment_lag_days < 1:
            raise ValueError("sentiment_lag_days 至少为 1，否则会用到当日情绪（未来函数）")


@dataclass(frozen=True)
class SentimentView:
    """扫描时点的市场情绪读数（来自涨停板情绪因子表）。"""

    available: bool
    sentiment_date: date | None = None
    seal_rate: float | None = None
    broken_rate: float | None = None
    max_streak: int | None = None
    limit_up_count: int | None = None
    exposure: float = 1.0
    stance: str = "unknown"
    label: str = "无情绪数据"
    note: str = ""


@dataclass(frozen=True)
class PickView:
    """单条买入候选。"""

    rank: int
    symbol: str
    name: str
    exchange: str
    board: str
    price: float
    previous_close: float
    change_pct: float
    #: 命中条件的权重和（0~100）。**保留原语义**：只回答「命中了哪几条、合计多少权重」，
    #: 因此同样命中 7 条的标的会并列（例如全是 86.0）。
    score: float
    #: 连续强度分（0~100）= Σ 权重 × 该条件的**连续强度**（见 `trigger_strengths`）。
    #: 2026-09-14 新增：原 `score` 是 0/1 打分，8 个条件只有 256 种组合，
    #: 实测盘中前 10 名**全部 86.0 并列**，排序实际退化成「按 20 日均额排」，
    #: 对「哪只更好」几乎没有信息量。强度分让排名有真正的梯度。
    strength_score: float
    triggers: tuple[str, ...]
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    entry_low: float
    entry_high: float
    stop_loss: float
    target_price: float
    risk_reward: float
    suggested_weight_pct: float
    atr14: float
    atr_pct: float
    ma20: float
    ma60: float
    momentum_20: float
    momentum_60: float
    volatility_20: float
    amount_20: float
    volume_ratio: float
    rsi14: float
    kdj_k: float
    kdj_d: float
    macd_hist: float
    boll_upper: float
    boll_lower: float
    bar_count: int
    last_bar_date: date
    live: bool


@dataclass(frozen=True)
class ScreenerResult:
    """一次扫描的完整结果。"""

    session_day: date
    signal_day: date
    bars_last_day: date | None
    live: bool
    generated_at: str
    #: 扫描完成时间（北京时间，ISO8601 带 +08:00）；UI 必须显示这个而不是 UTC，
    #: 否则休市日会出现「生成于 13:04」这种比本地时间早 8 小时的误导时间。
    generated_at_cst: str
    timezone: str
    scan_seconds: float
    universe_size: int
    quoted: int
    screened: int
    refined: int
    picks: list[PickView]
    sentiment: SentimentView
    # 本次实际使用的日线复权口径：qfq（前复权）/ none（不复权）
    bars_adjust: str = FALLBACK_BAR_ADJUST
    notes: tuple[str, ...] = ()
    config: ScreenerConfig = field(default_factory=ScreenerConfig)
    #: 行情覆盖率 = quoted / universe_size；低于门槛时 coverage_ok=False，
    #: 此时候选排序不构成研究依据（盘前实测曾出现 5/5550 仍返回 200）
    coverage_ratio: float = 0.0
    coverage_ok: bool = False


#: 覆盖率门槛：低于该比例视为「不足以支撑候选排序结论」
MIN_COVERAGE_RATIO = 0.5


def coverage_status(quoted: int, universe_size: int) -> tuple[float, bool, str]:
    """计算行情覆盖率并给出是否足以支撑结论（纯函数，便于测试）。

    背景（2026-09-14 盘前实测）：5 次扫描全部返回 200，但 5550 只标的里只有
    **5 只**取到行情（覆盖率 0.09%），却照样花 10~21 秒产出候选排序。研发计划
    要求「覆盖率不足拒绝给出结论」，因此这里显式判定并在 notes 里说明。
    """
    if universe_size <= 0:
        return 0.0, False, "股票池为空，无法评估行情覆盖率"
    ratio = quoted / universe_size
    if ratio >= MIN_COVERAGE_RATIO:
        return ratio, True, f"行情覆盖率 {ratio:.1%}（{quoted}/{universe_size}）"
    return (
        ratio,
        False,
        f"行情覆盖率仅 {ratio:.1%}（{quoted}/{universe_size}），不足以支撑候选排序结论："
        "结果仅供排查数据源问题，不得作为研究依据",
    )


@dataclass(frozen=True)
class SymbolSeries:
    """一只股票的日线序列（升序）。"""

    symbol: str
    days: tuple[date, ...]
    opens: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    amounts: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.closes)

    def replaced_or_extended(self, quote: QuoteData, day: date) -> tuple["SymbolSeries", bool]:
        """把行情并进序列，返回 (新序列, 是否带新一天数据)。

        与最后一根日线完全一致 → 原样返回 (self, False)：说明序列已经含当天
        （休市时行情就是最后一根日线；盘中且已入库当天时也是同一根）。
        """
        if len(self) == 0:
            return self, False
        if (
            quote.is_stale
            or not math.isfinite(quote.price)
            or quote.price <= 0
            or not math.isfinite(quote.volume)
            or quote.volume <= 0
        ):
            # 盘前集合竞价 / 停牌 / 本地缓存兜底的过期行情都构不成一根有效 K 线。
            # 尤其 volume<=0 时若强行追加当日 K 线，会把量比、成交额等因子算成 0。
            return self, False
        if (
            _same_volume(quote.volume, self.volumes[-1])
            and _same_value(quote.amount, self.amounts[-1])
            and _same_value(quote.price, self.closes[-1])
        ):
            return self, False
        bar = (
            day,
            quote.open or quote.price,
            quote.high or quote.price,
            quote.low or quote.price,
            quote.price,
            quote.volume,
            quote.amount,
        )
        if self.days[-1] >= day:
            # 最后一根已经是当天（盘中入库过）→ 用更新鲜的行情覆盖，避免同一天两根 K 线
            return (
                SymbolSeries(
                    symbol=self.symbol,
                    days=self.days[:-1] + (day,),
                    opens=self.opens[:-1] + (bar[1],),
                    highs=self.highs[:-1] + (bar[2],),
                    lows=self.lows[:-1] + (bar[3],),
                    closes=self.closes[:-1] + (bar[4],),
                    volumes=self.volumes[:-1] + (bar[5],),
                    amounts=self.amounts[:-1] + (bar[6],),
                ),
                True,
            )
        return (
            SymbolSeries(
                symbol=self.symbol,
                days=self.days + (day,),
                opens=self.opens + (bar[1],),
                highs=self.highs + (bar[2],),
                lows=self.lows + (bar[3],),
                closes=self.closes + (bar[4],),
                volumes=self.volumes + (bar[5],),
                amounts=self.amounts + (bar[6],),
            ),
            True,
        )

    def tail(self, count: int) -> "SymbolSeries":
        """取最后 count 根。"""
        if count >= len(self):
            return self
        return SymbolSeries(
            symbol=self.symbol,
            days=self.days[-count:],
            opens=self.opens[-count:],
            highs=self.highs[-count:],
            lows=self.lows[-count:],
            closes=self.closes[-count:],
            volumes=self.volumes[-count:],
            amounts=self.amounts[-count:],
        )


# ───────────────────────────── 触发器与风险标签 ─────────────────────────────

TRIGGER_LABELS: dict[str, str] = {
    "above_ma20": "站上 20 日线",
    "ma20_above_ma60": "20 日线在 60 日线上方",
    "momentum_positive": "20 日动量为正",
    "rsi_rebound": "RSI 落在 35~65 健康区",
    "macd_hist_turn": "MACD 柱状线转强",
    "boll_pullback": "回踩布林带中下轨",
    "stable_volatility": "波动率可控",
    "volume_expand": "成交量温和放大",
}

# 权重之和 = 100，``score`` 就是命中触发器的权重和，无需再做归一化
TRIGGER_WEIGHTS: dict[str, float] = {
    "above_ma20": 14.0,
    "ma20_above_ma60": 12.0,
    "momentum_positive": 10.0,
    "rsi_rebound": 12.0,
    "macd_hist_turn": 14.0,
    "boll_pullback": 14.0,
    "stable_volatility": 10.0,
    "volume_expand": 14.0,
}

# ── 连续强度（2026-09-14 新增） ───────────────────────────────────────────────
#
# 为什么需要：原打分是 0/1（命中给满权重），8 个条件只有 2^8=256 种组合。
# 实测（2026-09-14 13:39 盘中）：前 10 名**全部 86.0 分并列**，排序键退化为
# `amount_20`（20 日均额）→ 实际是「按成交额排」，对「哪只更好」几乎没有信息量。
#
# 现在的做法：把每个条件映射到 [0,1] 的**连续强度**，再按同一组权重加权：
#     strength_score = Σ TRIGGER_WEIGHTS[k] × strength[k]      （只在命中时计入）
# 于是同样命中 7 条的标的也会因「强度」不同而排出先后。
#
# 每条斜坡都是显式、可单独测试的（见 backend/tests/test_screener_strength.py），
# 参数用命名常量，便于日后按研究结论调整。

#: "站上 20 日线"：高出 3% 记满强度（再高不再加分，过度偏离由 extended 风险标记兜）
ABOVE_MA20_FULL_DEV = 0.03
#: "20 日线在 60 日线上方"：相差 2% 记满
MA20_ABOVE_MA60_FULL_DEV = 0.02
#: "20 日动量为正"：动量 10% 记满
MOMENTUM_FULL = 0.10
#: "RSI 健康区"：以 50 为中心，偏离 15 点强度归零（35/65 恰好为 0）
RSI_CENTER = 50.0
RSI_HALF_WIDTH = 15.0
#: "MACD 柱状线转强"：改善幅度达到股价的 0.2% 记满
MACD_TURN_FULL_PCT = 0.002
#: "回踩布林带"：位置 ≤ 该值才算命中；越接近下轨（0）强度越高
BOLL_PULLBACK_MAX_POS = 0.65
#: "波动率可控"：年化波动 0 记满、到上限记 0（命中阈值与强度上限共用，见下方 STABLE_VOL_CEILING）
#: "成交量温和放大"：量比 1.2 记 0、2.0 记满（同上，共用 VOLUME_EXPAND_MIN）


def _ramp(value: float, full_at: float, zero_at: float) -> float:
    """线性斜坡：``zero_at`` 处为 0、``full_at`` 处为 1，两端截断。"""
    if full_at == zero_at:
        return 0.0
    ratio = (value - zero_at) / (full_at - zero_at)
    return max(0.0, min(1.0, ratio))


def trigger_strengths(
    *,
    price: float,
    ma20: float,
    ma60: float,
    momentum_20: float,
    rsi14: float,
    macd_hist: float,
    macd_hist_prev: float,
    boll_position: float | None,
    volatility_20: float,
    volume_ratio_20: float,
) -> dict[str, float]:
    """把 8 个触发条件映射成 [0,1] 的**连续强度**（0 表示未命中）。

    每条的含义与斜坡都在上方常量处写明；这里只做映射，不做取舍判断 ——
    「是否命中」仍由 `ScreenerService._refine` 里那套阈值判定，两者共用同一组常量。

    需要精确理解的不变式（不要写成"强度为 0 当且仅当未命中"，那样是错的）：

    * **强度 > 0 ⇒ 该条件必然命中** —— 保证不会出现"没命中却拿到强度分"；
    * **命中 ⇒ 强度 ≥ 0**，但在阈值**边界**上强度可以是 0：
      price == ma20、ma20 == ma60、RSI 恰为 35/65、布林位置恰为 0.65、
      年化波动恰为 0.45、量比恰为 1.2。这些是测度为零的临界点，
      在 `_refine` 里仍按命中计入 `score`（历史口径不变），只是不贡献 `strength_score`。
    * 因此 **strength_score ≤ score 恒成立**。
    """
    strengths: dict[str, float] = {}

    # 1) 站上 20 日线：按高出幅度
    strengths["above_ma20"] = (
        _ramp(price / ma20 - 1.0, ABOVE_MA20_FULL_DEV, 0.0) if ma20 > 0 else 0.0
    )
    # 2) 20 日线在 60 日线上方：按相差幅度
    strengths["ma20_above_ma60"] = (
        _ramp(ma20 / ma60 - 1.0, MA20_ABOVE_MA60_FULL_DEV, 0.0) if ma60 > 0 else 0.0
    )
    # 3) 20 日动量为正：按动量大小
    strengths["momentum_positive"] = _ramp(momentum_20, MOMENTUM_FULL, 0.0)
    # 4) RSI 健康区：以 50 为峰、向 35/65 递减（"越中性越健康"）
    strengths["rsi_rebound"] = (
        max(0.0, 1.0 - abs(rsi14 - RSI_CENTER) / RSI_HALF_WIDTH)
        if 35.0 <= rsi14 <= 65.0
        else 0.0
    )
    # 5) MACD 柱状线转强：按相对股价的改善幅度
    if price > 0 and macd_hist > macd_hist_prev and macd_hist > -0.01 * price:
        strengths["macd_hist_turn"] = _ramp(
            (macd_hist - macd_hist_prev) / price, MACD_TURN_FULL_PCT, 0.0
        )
    else:
        strengths["macd_hist_turn"] = 0.0
    # 6) 回踩布林带：位置越低（越靠下轨）越强
    if boll_position is not None and boll_position <= BOLL_PULLBACK_MAX_POS:
        strengths["boll_pullback"] = _ramp(
            boll_position, 0.0, BOLL_PULLBACK_MAX_POS
        )
    else:
        strengths["boll_pullback"] = 0.0
    # 7) 波动率可控：越低越强（上限处为 0）
    if 0.0 <= volatility_20 <= STABLE_VOL_CEILING:
        strengths["stable_volatility"] = _ramp(
            volatility_20, 0.0, STABLE_VOL_CEILING
        )
    else:
        strengths["stable_volatility"] = 0.0
    # 8) 成交量温和放大：量比 1.2 起算、2.0 记满
    if volume_ratio_20 >= VOLUME_EXPAND_MIN:
        strengths["volume_expand"] = _ramp(
            volume_ratio_20, VOLUME_EXPAND_FULL, VOLUME_EXPAND_MIN
        )
    else:
        strengths["volume_expand"] = 0.0
    return strengths


RISK_LABELS: dict[str, str] = {
    "near_limit_up": "接近涨停",
    "high_volatility": "波动偏大",
    "overbought": "短线超买",
    "extended": "20 日涨幅偏大",
    "thin_liquidity": "流动性偏低",
    "poor_risk_reward": "盈亏比偏低",
}

BOARD_LABELS: dict[str, str] = {
    "main": "主板",
    "gem": "创业板",
    "star": "科创板",
    "bse": "北交所",
    "unknown": "未知",
}

# 风险阈值：全部是固定、可解释的口径，不随行情自适应
HIGH_ATR_PCT = 6.0  # ATR / 现价 > 6% 视为高波动
OVERBOUGHT_RSI = 75.0  # RSI > 75 视为短线超买
EXTENDED_MOMENTUM = 0.30  # 20 日涨幅 > 30% 视为短期涨幅偏大
NEAR_LIMIT_RATIO = 0.98  # 距涨停价 2% 以内视为接近涨停
MIN_RISK_REWARD = 1.0  # 盈亏比低于 1 视为不划算
STABLE_VOL_CEILING = 0.45  # 年化波动率上限（可控区间）
#: 成交量「温和放大」的量比区间：1.2 起算、2.0 记满（命中阈值与强度斜坡共用同一常量，
#: 避免出现「判定命中但强度为 0」的不一致）
VOLUME_EXPAND_MIN = 1.2
VOLUME_EXPAND_FULL = 2.0
STOP_WIDTH_FLOOR = 0.70  # 止损最宽不超过入场价的 30%
EXPOSURE_FLOOR = 0.3  # 情绪差时的最低仓位系数
# 日线读取的自然日回溯系数：120 个交易日约 175 个自然日，留 1.9 倍 + 20 天余量
CALENDAR_LOOKBACK_FACTOR = 1.9
CALENDAR_LOOKBACK_EXTRA_DAYS = 20


class ScreenerUnavailable(RuntimeError):
    """扫描所需的本地数据缺失（交易日历为空 / 股票池为空 / 行情全失败）。"""


@dataclass(frozen=True)
class _Pass1:
    """第一段（全市场，便宜因子）算出的中间结果。"""

    symbol: str
    name: str
    exchange: str
    board: str
    is_st: bool
    series: SymbolSeries
    live: bool
    price: float
    previous_close: float
    change_pct: float
    ma20: float
    ma60: float
    momentum_20: float
    momentum_60: float
    amount_20: float
    high20: float
    low20: float
    volatility_20: float
    volume_ratio_20: float


def _last_value(series: list[float]) -> float | None:
    """最后一个有效（非 NaN）值。"""
    return latest_valid(series)


def _or_default(value: float | None, default: float) -> float:
    return default if value is None else value


def _split_even(symbols: list[str], parts: int) -> list[list[str]]:
    """把列表切成尽量均匀的 parts 段（段数可能少于 parts）。"""
    if parts <= 1 or not symbols:
        return [symbols]
    size = math.ceil(len(symbols) / parts)
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


class ScreenerService:
    """全市场两段式实时扫描。

    第 1 段（便宜，全市场约 0.1s）：120 根日线 + 实时行情 → 均线 / 动量 /
    成交额 / 波动率等基础因子 → 硬性过滤 → 按强弱取 ``refine_pool`` 只入围；
    第 2 段（贵重，约 1s / 200 只）：只对入围池精算 RSI / BOLL / KDJ / MACD /
    ATR → 触发器打分 → 风险标签 → 仓位与止损计划。

    行情默认走「若干独立 TdxProvider 连接并行 + ProviderManager 兜底」：实测全
    池 5550 只 5 连接并行 1.34s，单连接 4.93s。也可以注入 ``quote_fetcher``
    用于测试或替换数据源。

    本模块只读：不写数据库、不改动任何现有信号与策略逻辑。
    """

    def __init__(
        self,
        provider_manager: ProviderManager | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
        quote_fetcher: QuoteFetcher | None = None,
        tdx_pool_size: int = 5,
        clock: Callable[[], date] = date.today,
    ) -> None:
        self._manager = provider_manager
        self._session_factory = session_factory or SessionLocal
        self._quote_fetcher = quote_fetcher
        self._clock = clock
        self._pool_size = max(0, int(tdx_pool_size))
        self._pool: list[TdxProvider] = []
        self._pool_lock = asyncio.Lock()
        self._bars_cache_key: tuple[object, ...] | None = None
        self._bars_cache: dict[str, SymbolSeries] = {}
        self._bars_cache_adjust: str = FALLBACK_BAR_ADJUST
        # 复权口径/失效键的短 TTL 缓存。实测（2026-09-14 盘中）：resolve_bar_adjust
        # 的 `GROUP BY adjust` 聚合在 1335 万行上要 ~6 秒，而它**每次扫描都会执行**
        # （要先算出缓存键才知道日线缓存能不能用），于是「缓存命中」也仍然慢 6 秒。
        # 日线数据在盘中不会被本进程写入，60 秒 TTL 足以把这段成本摊销掉。
        self._adjust_cache: tuple[float, str, int, date | None] | None = None
        # 扫描合并（single-flight）：实测（2026-09-14 连续竞价）全市场扫描
        # P50 32.8s / P95 66.3s，而前端默认 60 秒自动刷新 —— 不做合并时并发请求
        # 会各自再跑一遍全市场扫描，互相拖慢并放大数据源压力。
        # 同一份参数的并发调用共享同一次扫描；参数不同的请求仍各自执行（互不阻塞）。
        self._inflight: dict[str, asyncio.Task[ScreenerResult]] = {}
        self._coalesced_scans = 0

    @property
    def coalesced_scans(self) -> int:
        """被合并（共享已有扫描）的请求数，供可观测性使用。"""
        return self._coalesced_scans

    @staticmethod
    def _config_cache_key(config: ScreenerConfig) -> str:
        import json

        return json.dumps(config.__dict__, sort_keys=True, default=str)

    # ─────────── 生命周期 ───────────

    async def warm_bars_cache(self, lookback: int = DEFAULT_LOOKBACK_DAYS) -> float:
        """预热日线缓存，返回耗时（秒）；失败不影响服务。

        为什么需要：实测冷启动读 66 万行日线约 15.9 秒，用户「启动后第一次点开
        首页」就要等这么久。启动后在后台预热，把这段成本挪到无人等待的时刻。
        """
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self._load_series_in_thread, lookback)
        except Exception as exc:  # noqa: BLE001 - 预热失败只记录
            logger.warning("日线缓存预热失败：%s", exc)
        elapsed = time.perf_counter() - started
        logger.info("日线缓存预热完成：%.1fs", elapsed)
        return elapsed

    async def close(self) -> None:
        """关闭扫描器自建的行情连接池（应用关停时调用）。"""
        pool, self._pool = self._pool, []
        for provider in pool:
            try:
                await provider.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响关停
                logger.warning("关闭买点雷达行情连接失败：%s", exc)

    # ─────────── 行情 ───────────

    async def _acquire_pool(self) -> list[TdxProvider]:
        """惰性创建并复用一组互相独立的通达信连接。"""
        if self._pool_size <= 0:
            return []
        async with self._pool_lock:
            if not self._pool:
                self._pool = [TdxProvider() for _ in range(self._pool_size)]
            return list(self._pool)

    async def _fetch_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if self._quote_fetcher is not None:
            return await self._quote_fetcher(list(symbols))
        return await self._pooled_quotes(symbols)

    async def _pooled_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            return {}
        pool = await self._acquire_pool()
        if not pool:
            return await self._fallback_quotes(symbols)
        chunks = _split_even(symbols, len(pool))
        results = await asyncio.gather(
            *(provider.get_quotes(chunk) for provider, chunk in zip(pool, chunks)),
            return_exceptions=True,
        )
        quotes: dict[str, QuoteData] = {}
        for chunk, result in zip(chunks, results):
            if isinstance(result, dict):
                quotes.update(result)
            else:
                logger.warning("买点雷达行情分片失败（%d 只）：%s", len(chunk), result)
        missing = [symbol for symbol in symbols if symbol not in quotes]
        if missing:
            logger.info("买点雷达 %d 只标的改用兜底数据源", len(missing))
            quotes.update(await self._fallback_quotes(missing))
        return quotes

    async def _fallback_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols or self._manager is None:
            return {}
        try:
            return await self._manager.get_quotes(list(symbols))
        except Exception as exc:  # noqa: BLE001 - 兜底失败按「该标的无行情」处理
            logger.warning("买点雷达兜底行情失败：%s", exc)
            return {}

    # ─────────── 主流程 ───────────

    async def run(self, config: ScreenerConfig | None = None) -> ScreenerResult:
        """执行一次扫描；**同参数的并发调用共享同一次扫描**（single-flight）。

        为什么需要：2026-09-14 连续竞价实测全市场扫描 P50 32.8s / P95 66.3s，
        而首页默认 60 秒自动刷新；不合并时自动刷新与手动刷新会并发跑多轮全市场
        扫描，互相拖慢并放大数据源压力。
        """
        cfg = config or ScreenerConfig()
        cfg.validate()
        key = self._config_cache_key(cfg)
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            self._coalesced_scans += 1
            logger.info("扫描合并：复用进行中的同参数扫描（第 %d 次）", self._coalesced_scans)
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._run_scan(cfg))
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _run_scan(self, cfg: ScreenerConfig) -> ScreenerResult:
        """实际执行一次全市场扫描。"""
        started = time.perf_counter()
        notes: list[str] = []

        with self._session_factory() as db:
            calendar = TradingCalendar(db)
            if calendar.is_empty():
                raise ScreenerUnavailable("本地交易日历为空，无法判断交易日")
            today = self._clock()
            session_day, signal_day = self._resolve_days(calendar, today)
            if session_day != today:
                notes.append(
                    f"今日（{today.isoformat()}）休市，买点按下一交易日 "
                    f"{signal_day.isoformat()} 评估"
                )
            universe, snapshot_day = self._load_universe(db)
            if not universe:
                raise ScreenerUnavailable(
                    "本地股票池为空，请先调用 POST /api/universe/sync"
                )
            sentiment = self._sentiment_view(db, signal_day, cfg)
            rules = MarketRuleEngine()

        # 日线读取（冷启动约 2.4s，66 万行）与行情抓取（约 1.8s）互不依赖，并行执行
        bars_task = asyncio.create_task(
            asyncio.to_thread(self._load_series_in_thread, cfg.lookback_days)
        )
        quotes_started = time.perf_counter()
        try:
            quotes = await self._fetch_quotes(list(universe))
        except BaseException:
            await asyncio.gather(bars_task, return_exceptions=True)
            raise
        quote_seconds = time.perf_counter() - quotes_started
        series_map, bars_last_day, bars_seconds, bars_adjust = await bars_task
        if not quotes:
            raise ScreenerUnavailable("所有数据源都没有返回行情，请稍后重试")
        notes.append(
            f"股票池快照 {snapshot_day.isoformat()}，可交易 A 股 {len(universe)} 只，"
            f"本次取到行情 {len(quotes)} 只"
        )

        pass1: list[_Pass1] = []
        live_count = 0
        no_quote = 0
        screen_started = time.perf_counter()
        for symbol, meta in universe.items():
            series = series_map.get(symbol)
            if series is None or len(series) < MIN_BARS_FOR_FACTORS:
                continue
            live = False
            quote = quotes.get(symbol)
            if quote is None:
                no_quote += 1
            else:
                series, live = series.replaced_or_extended(quote, session_day)
                if live:
                    live_count += 1
            item = self._pass1_factors(symbol, meta, series, quote, live)
            if item is not None:
                pass1.append(item)
        screen_seconds = time.perf_counter() - screen_started

        if live_count:
            notes.append(
                f"{live_count} 只标的的行情带来了 {session_day.isoformat()} 的当日 K 线"
            )
        else:
            label = bars_last_day.isoformat() if bars_last_day else "未知"
            notes.append(f"行情与本地日线一致，按最近交易日（{label}）收盘数据评估")
        if no_quote:
            notes.append(f"{no_quote} 只标的没有取到行情，已跳过")

        eligible = [item for item in pass1 if self._passes_filters(item, cfg)]
        eligible.sort(key=_prescreen_key, reverse=True)
        pool = eligible[: cfg.refine_pool]

        picks: list[PickView] = []
        refine_started = time.perf_counter()
        for item in pool:
            pick = self._refine(item, rules, cfg, sentiment)
            if pick is not None:
                picks.append(pick)
        # 排名：先按**连续强度分**（有梯度），再按命中权重和（同强度时的粗分），
        # 最后才用 20 日均额（流动性）与代码兜底 —— 2026-09-14 之前只有后两者，
        # 实测盘中前 10 名全部 86.0 并列，排序实际退化成「按成交额排」。
        picks.sort(
            key=lambda p: (-p.strength_score, -p.score, -p.amount_20, p.symbol)
        )
        picks = [
            replace(pick, rank=index + 1)
            for index, pick in enumerate(picks[: cfg.top_n])
        ]
        refine_seconds = time.perf_counter() - refine_started

        notes.append(
            f"技术指标窗口 {cfg.lookback_days} 根日线（{ADJUST_LABELS.get(bars_adjust, bars_adjust)}）；"
            "与「技术指标」页 "
            "250 根口径实测偏差 RSI<0.1、ATR<0.1%"
        )
        notes.append(
            f"阶段耗时（行情与日线并行）：行情 {quote_seconds:.1f}s、日线 "
            f"{bars_seconds:.1f}s，初筛 {screen_seconds:.1f}s、精算 "
            f"{refine_seconds:.1f}s"
        )
        coverage_ratio, coverage_ok, coverage_note = coverage_status(
            len(quotes), len(universe)
        )
        # 覆盖率说明放在免责声明**之前**：免责声明约定为最后一条 notes
        notes.append(coverage_note)
        if not coverage_ok:
            notes.append(
                "覆盖率不足：本次候选排序不构成研究依据，请先排查数据源可用性"
                "（盘前/数据源故障时常见）"
            )
        notes.append(
            "排名口径：先按**强度分**（strength_score，0~100 = Σ 权重 × 连续强度）排序，"
            "再按命中权重和（score）与 20 日均额（流动性）兜底。"
            "score 只反映「命中了哪几条」，同样命中 7 条的标的会并列（例如都是 86.0），"
            "不能单独用于比较优劣。"
        )
        notes.append("分析结果仅用于研究，不构成投资建议")
        return ScreenerResult(
            session_day=session_day,
            signal_day=signal_day,
            bars_last_day=bars_last_day,
            live=live_count > 0,
            generated_at=utc_now().isoformat(),
            generated_at_cst=now_cst().isoformat(timespec="seconds"),
            timezone="Asia/Shanghai (UTC+8, 无夏令时)",
            scan_seconds=round(time.perf_counter() - started, 3),
            universe_size=len(universe),
            quoted=len(quotes),
            coverage_ratio=round(coverage_ratio, 4),
            coverage_ok=coverage_ok,
            screened=len(pass1),
            refined=len(pool),
            picks=picks,
            sentiment=sentiment,
            bars_adjust=bars_adjust,
            notes=tuple(notes),
            config=cfg,
        )

    @staticmethod
    def _resolve_days(calendar: TradingCalendar, today: date) -> tuple[date, date]:
        """返回 (行情所属交易日, 信号交易日)。

        交易日 → 两者都是今天；休市日 → 行情按最近交易日、买点按下一交易日。
        """
        if calendar.is_trading_day(today):
            return today, today
        session_day = calendar.last_trading_day_on_or_before(today)
        try:
            signal_day = calendar.next_trading_day(session_day)
        except ValueError:
            signal_day = session_day
        return session_day, signal_day

    # ─────────── 数据加载 ───────────

    def _load_series_in_thread(
        self, lookback: int
    ) -> tuple[dict[str, SymbolSeries], date | None, float, str]:
        """在独立线程 + 独立会话里读日线（供 ``asyncio.to_thread`` 并行调用）。"""
        started = time.perf_counter()
        with self._session_factory() as db:
            series_map, last_day, adjust = self._load_series(db, lookback)
        return series_map, last_day, time.perf_counter() - started, adjust

    @staticmethod
    def _load_universe(db: Session) -> tuple[dict[str, dict[str, object]], date]:
        """取最近一次快照里「可交易」的成分股（退市 / 停牌 / 待上市已排除）。"""
        head = db.execute(
            select(UniverseSnapshot.id, UniverseSnapshot.trading_day)
            .order_by(UniverseSnapshot.trading_day.desc())
            .limit(1)
        ).first()
        if head is None:
            return {}, date.min
        snapshot_id, snapshot_day = int(head[0]), head[1]
        rows = db.execute(
            select(
                UniverseMember.symbol,
                UniverseMember.name,
                UniverseMember.exchange,
                UniverseMember.board,
                UniverseMember.is_st,
            ).where(
                UniverseMember.snapshot_id == snapshot_id,
                UniverseMember.is_included.is_(True),
                UniverseMember.trading_status == "active",
            )
        ).all()
        universe: dict[str, dict[str, object]] = {}
        for symbol, name, exchange, board, is_st in rows:
            universe[symbol] = {
                "name": name or "",
                "exchange": (exchange or "").upper(),
                "board": board or "unknown",
                "is_st": bool(is_st),
            }
        return universe, snapshot_day

    def _resolve_adjust_cached(
        self, db: Session
    ) -> tuple[str, int, date | None]:
        """复权口径解析；转发到**进程级共享**缓存（见 ``resolve_bar_adjust_cached``）。"""
        return resolve_bar_adjust_cached(db, BAR_PERIOD)

    def _load_series(
        self, db: Session, lookback: int
    ) -> tuple[dict[str, SymbolSeries], date | None, str]:
        """读取最近 lookback 根日线（前复权优先）；按 (口径, 行数, 末日, 窗口) 缓存。

        先用 ``trade_date`` 下界（自然日回溯 1.9 倍 + 20 天余量）缩小扫描范围，
        再用 ``ROW_NUMBER`` 取每只标的最近 lookback 根，避免把整表读进内存。
        """
        # MAX(id) / MAX(trade_date) 都走索引，用来判断缓存是否失效；
        # 比 COUNT(*) 快一个数量级（实测 0.18s → 0.00s）。
        adjust, max_id, last_day = self._resolve_adjust_cached(db)
        key: tuple[object, ...] = (adjust, max_id, last_day, lookback)
        if key == self._bars_cache_key:
            # 可观测性：日线读取是盘中扫描的主要成本之一（实测冷启动 15.9s、
            # 命中缓存应≈0），必须能分辨「慢在没命中」还是「慢在别处」。
            logger.info("日线缓存命中：%d 只标的", len(self._bars_cache))
            return self._bars_cache, last_day, self._bars_cache_adjust
        logger.info(
            "日线缓存未命中：重新读取（key=%s，上次 key=%s）", key, self._bars_cache_key
        )
        if last_day is None:
            self._bars_cache_key, self._bars_cache = key, {}
            self._bars_cache_adjust = adjust
            return {}, None, adjust

        floor = last_day - timedelta(
            days=int(lookback * CALENDAR_LOOKBACK_FACTOR)
            + CALENDAR_LOOKBACK_EXTRA_DAYS
        )
        windowed = (
            select(
                HistoricalBar.symbol.label("symbol"),
                HistoricalBar.trade_date.label("trade_date"),
                HistoricalBar.open.label("open"),
                HistoricalBar.high.label("high"),
                HistoricalBar.low.label("low"),
                HistoricalBar.close.label("close"),
                HistoricalBar.volume.label("volume"),
                HistoricalBar.amount.label("amount"),
                func.row_number()
                .over(
                    partition_by=HistoricalBar.symbol,
                    order_by=HistoricalBar.trade_date.desc(),
                )
                .label("rn"),
            )
            .where(
                HistoricalBar.period == BAR_PERIOD,
                HistoricalBar.adjust == adjust,
                HistoricalBar.trade_date >= floor,
            )
            .subquery()
        )
        rows = db.execute(
            select(
                windowed.c.symbol,
                windowed.c.trade_date,
                windowed.c.open,
                windowed.c.high,
                windowed.c.low,
                windowed.c.close,
                windowed.c.volume,
                windowed.c.amount,
            )
            .where(windowed.c.rn <= lookback)
            .order_by(windowed.c.symbol, windowed.c.trade_date)
        ).all()

        buckets: dict[str, dict[str, list]] = {}
        for symbol, trade_date, open_, high, low, close, volume, amount in rows:
            bucket = buckets.get(symbol)
            if bucket is None:
                bucket = {
                    "days": [],
                    "opens": [],
                    "highs": [],
                    "lows": [],
                    "closes": [],
                    "volumes": [],
                    "amounts": [],
                }
                buckets[symbol] = bucket
            bucket["days"].append(trade_date)
            bucket["opens"].append(float(open_ or 0.0))
            bucket["highs"].append(float(high or 0.0))
            bucket["lows"].append(float(low or 0.0))
            bucket["closes"].append(float(close or 0.0))
            bucket["volumes"].append(float(volume or 0.0))
            bucket["amounts"].append(float(amount or 0.0))

        series_map = {
            symbol: SymbolSeries(
                symbol=symbol,
                days=tuple(bucket["days"]),
                opens=tuple(bucket["opens"]),
                highs=tuple(bucket["highs"]),
                lows=tuple(bucket["lows"]),
                closes=tuple(bucket["closes"]),
                volumes=tuple(bucket["volumes"]),
                amounts=tuple(bucket["amounts"]),
            )
            for symbol, bucket in buckets.items()
        }
        self._bars_cache_key, self._bars_cache = key, series_map
        self._bars_cache_adjust = adjust
        return series_map, last_day, adjust

    # ─────────── 第 1 段：便宜因子 + 硬性过滤 ───────────

    @staticmethod
    def _pass1_factors(
        symbol: str,
        meta: dict[str, object],
        series: SymbolSeries,
        quote: QuoteData | None,
        live: bool,
    ) -> _Pass1 | None:
        """用日线尾部算基础因子；任一因子算不出来就跳过该标的。"""
        closes = series.closes
        price = closes[-1]
        if not math.isfinite(price) or price <= 0:
            return None
        previous_close = (
            quote.previous_close
            if quote is not None and quote.previous_close > 0
            else (closes[-2] if len(closes) >= 2 else price)
        )
        try:
            ma20 = _require(fm.mean(closes, 20))
            ma60 = _require(fm.mean(closes, 60))
            momentum_20 = _require(fm.momentum(closes, 20))
            momentum_60 = _require(fm.momentum(closes, 60))
            amount_20 = _require(fm.mean(series.amounts, 20))
            high20 = _require(fm.rolling_high(series.highs, 20))
            low20 = _require(fm.rolling_low(series.lows, 20))
            volatility_20 = _require(fm.annualized_volatility(closes, 20))
            volume_ratio_20 = _require(fm.volume_ratio(series.volumes, 20))
        except _MissingFactor:
            return None
        change_pct = (
            (price / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0
        )
        return _Pass1(
            symbol=symbol,
            name=str(meta.get("name") or ""),
            exchange=str(meta.get("exchange") or ""),
            board=str(meta.get("board") or "unknown"),
            is_st=bool(meta.get("is_st")),
            series=series,
            live=live,
            price=price,
            previous_close=previous_close,
            change_pct=change_pct,
            ma20=ma20,
            ma60=ma60,
            momentum_20=momentum_20,
            momentum_60=momentum_60,
            amount_20=amount_20,
            high20=high20,
            low20=low20,
            volatility_20=volatility_20,
            volume_ratio_20=volume_ratio_20,
        )

    @staticmethod
    def _passes_filters(item: _Pass1, cfg: ScreenerConfig) -> bool:
        """硬性过滤：成交额 / 价格 / 涨跌幅 / ST。"""
        if item.amount_20 < cfg.min_amount_20:
            return False
        if not cfg.min_price <= item.price <= cfg.max_price:
            return False
        if not cfg.min_change_pct <= item.change_pct <= cfg.max_change_pct:
            return False
        if cfg.exclude_st and item.is_st:
            return False
        return True

    # ─────────── 第 2 段：精算指标 + 打分 + 仓位 ───────────

    @staticmethod
    def _refine(
        item: _Pass1,
        rules: MarketRuleEngine,
        cfg: ScreenerConfig,
        sentiment: SentimentView,
    ) -> PickView | None:
        """精算 RSI / BOLL / KDJ / MACD / ATR，打分并给出仓位与止损计划。"""
        window = item.series.tail(cfg.lookback_days)
        closes = list(window.closes)
        highs = list(window.highs)
        lows = list(window.lows)

        rsi14 = _or_default(_last_value(rsi(closes, 14)), 50.0)
        k_value, d_value, _ = kdj(highs, lows, closes, 9, 3, 3)
        kdj_k = _or_default(_last_value(k_value), 50.0)
        kdj_d = _or_default(_last_value(d_value), 50.0)
        _, _, hist = macd(closes, 12, 26, 9)
        macd_hist = _or_default(_last_value(hist), 0.0)
        macd_hist_prev = _or_default(_last_value(hist[:-1]), macd_hist)
        boll_upper, boll_middle, boll_lower = boll(closes, 20, 2.0)
        upper = _last_value(boll_upper)
        middle = _last_value(boll_middle)
        lower = _last_value(boll_lower)
        atr14 = _or_default(_last_value(atr(highs, lows, closes, 14)), 0.0)
        if atr14 <= 0 or item.price <= 0:
            return None
        atr_pct = atr14 / item.price * 100.0

        hits = {
            "above_ma20": item.price >= item.ma20,
            "ma20_above_ma60": item.ma20 >= item.ma60,
            "momentum_positive": item.momentum_20 > 0,
            "rsi_rebound": 35.0 <= rsi14 <= 65.0,
            "macd_hist_turn": (
                macd_hist > macd_hist_prev and macd_hist > -0.01 * item.price
            ),
            "boll_pullback": (
                middle is not None
                and item.price <= middle * 1.02
                and fm.boll_position(item.price, upper, lower) is not None
                and (fm.boll_position(item.price, upper, lower) or 0.0) <= 0.65
            ),
            "stable_volatility": item.volatility_20 <= STABLE_VOL_CEILING,
            "volume_expand": item.volume_ratio_20 >= VOLUME_EXPAND_MIN,
        }
        triggers = tuple(key for key, hit in hits.items() if hit)
        if len(triggers) < cfg.min_triggers:
            return None
        score = round(sum(TRIGGER_WEIGHTS[key] for key in triggers), 2)
        # 连续强度分（0~100）：让「同样命中 7 条」的标的也能排出先后。
        # 强度只对**命中**的条件计入，因此 strength_score ≤ score 恒成立。
        strengths = trigger_strengths(
            price=item.price,
            ma20=item.ma20,
            ma60=item.ma60,
            momentum_20=item.momentum_20,
            rsi14=rsi14,
            macd_hist=macd_hist,
            macd_hist_prev=macd_hist_prev,
            boll_position=fm.boll_position(item.price, upper, lower),
            volatility_20=item.volatility_20,
            volume_ratio_20=item.volume_ratio_20,
        )
        strength_score = round(
            sum(TRIGGER_WEIGHTS[key] * strengths.get(key, 0.0) for key in triggers), 2
        )

        risk_flags = ScreenerService._risk_flags(item, rules, cfg, rsi14, atr_pct)
        entry_low, entry_high, entry_ref = ScreenerService._entry_band(item.price, atr14)
        stop_loss = ScreenerService._stop_loss(item, entry_ref, atr14, cfg)
        target_price = entry_ref + cfg.target_atr_multiple * atr14
        risk = entry_ref - stop_loss
        risk_reward = round((target_price - entry_ref) / risk, 2) if risk > 0 else 0.0
        if risk_reward < MIN_RISK_REWARD:
            risk_flags.append("poor_risk_reward")

        stop_pct = (entry_ref - stop_loss) / entry_ref * 100.0 if entry_ref > 0 else 0.0
        if stop_pct <= 0:
            return None
        exposure = (
            sentiment.exposure if cfg.scale_exposure_by_sentiment else 1.0
        )
        weight = min(cfg.max_weight_pct, cfg.risk_budget_pct / (stop_pct / 100.0))
        weight = round(weight * exposure, 2)
        if weight <= 0:
            return None

        return PickView(
            rank=0,
            symbol=item.symbol,
            name=item.name,
            exchange=item.exchange,
            board=item.board,
            price=round(item.price, 2),
            previous_close=round(item.previous_close, 2),
            change_pct=round(item.change_pct, 2),
            score=score,
            strength_score=strength_score,
            triggers=triggers,
            reasons=tuple(TRIGGER_LABELS[key] for key in triggers),
            risk_flags=tuple(risk_flags),
            entry_low=round(max(entry_low, 0.01), 2),
            entry_high=round(max(entry_high, 0.01), 2),
            stop_loss=round(max(stop_loss, 0.01), 2),
            target_price=round(max(target_price, 0.01), 2),
            risk_reward=risk_reward,
            suggested_weight_pct=weight,
            atr14=round(atr14, 3),
            atr_pct=round(atr_pct, 2),
            ma20=round(item.ma20, 2),
            ma60=round(item.ma60, 2),
            momentum_20=round(item.momentum_20, 4),
            momentum_60=round(item.momentum_60, 4),
            volatility_20=round(item.volatility_20, 4),
            amount_20=round(item.amount_20, 2),
            volume_ratio=round(item.volume_ratio_20, 2),
            rsi14=round(rsi14, 2),
            kdj_k=round(kdj_k, 2),
            kdj_d=round(kdj_d, 2),
            macd_hist=round(macd_hist, 4),
            boll_upper=round(upper, 2) if upper is not None else 0.0,
            boll_lower=round(lower, 2) if lower is not None else 0.0,
            bar_count=len(item.series),
            last_bar_date=item.series.days[-1],
            live=item.live,
        )

    @staticmethod
    def _risk_flags(
        item: _Pass1,
        rules: MarketRuleEngine,
        cfg: ScreenerConfig,
        rsi14: float,
        atr_pct: float,
    ) -> list[str]:
        """风险标签：只做提示，不参与分数。"""
        flags: list[str] = []
        if item.previous_close > 0:
            limit_up = rules.get_rules(
                item.symbol, item.name, item.is_st
            ).limit_up(item.previous_close)
            if limit_up is not None and item.price >= float(limit_up) * NEAR_LIMIT_RATIO:
                flags.append("near_limit_up")
        if atr_pct > HIGH_ATR_PCT:
            flags.append("high_volatility")
        if rsi14 > OVERBOUGHT_RSI:
            flags.append("overbought")
        if item.momentum_20 > EXTENDED_MOMENTUM:
            flags.append("extended")
        if item.amount_20 < cfg.min_amount_20 * 2.0:
            flags.append("thin_liquidity")
        return flags

    @staticmethod
    def _entry_band(price: float, atr14: float) -> tuple[float, float, float]:
        """建议挂单区间：现价下方 0.5 ATR ~ 上方 0.2 ATR。"""
        entry_low = price - 0.5 * atr14
        entry_high = price + 0.2 * atr14
        return entry_low, entry_high, (entry_low + entry_high) / 2.0

    @staticmethod
    def _stop_loss(
        item: _Pass1, entry_ref: float, atr14: float, cfg: ScreenerConfig
    ) -> float:
        """止损：ATR 止损与 20 日低点下方取更宽的一个，但不超过入场价 30%。"""
        atr_stop = entry_ref - cfg.stop_atr_multiple * atr14
        struct_stop = item.low20 * 0.99
        stop = min(atr_stop, struct_stop)
        stop = max(stop, entry_ref * STOP_WIDTH_FLOOR)
        return min(stop, entry_ref * 0.999)

    # ─────────── 市场情绪 ───────────

    @staticmethod
    def _sentiment_view(
        db: Session, signal_day: date, cfg: ScreenerConfig
    ) -> SentimentView:
        """取信号日之前（滞后 lag 个交易日）的涨停板情绪因子。"""
        rows = db.execute(
            select(
                LimitUpSentiment.trade_date,
                LimitUpSentiment.limit_up_count,
                LimitUpSentiment.broken_board_count,
                LimitUpSentiment.seal_rate,
                LimitUpSentiment.broken_rate,
                LimitUpSentiment.max_streak,
            )
            .where(LimitUpSentiment.trade_date <= signal_day)
            .order_by(LimitUpSentiment.trade_date.desc())
            .limit(max(cfg.sentiment_lag_days + 5, 10))
        ).all()
        if not rows:
            return SentimentView(
                available=False,
                exposure=1.0,
                stance="unknown",
                label="无情绪数据",
                note="本地还没有涨停板情绪因子，建议先抓取或回算",
            )
        snapshots = [
            SentimentSnapshot(
                trade_date=row[0],
                limit_up_count=int(row[1] or 0),
                broken_board_count=int(row[2] or 0),
                seal_rate=row[3],
                broken_rate=row[4],
                max_streak=int(row[5] or 0),
            )
            for row in rows
        ]
        gate = SentimentGate(
            SentimentGateConfig(
                enabled=True,
                lag_days=cfg.sentiment_lag_days,
                scale_exposure=cfg.scale_exposure_by_sentiment,
                min_exposure=EXPOSURE_FLOOR,
            ),
            snapshots,
        )
        decision = gate.decision(signal_day)
        if decision.sentiment_date is None:
            return SentimentView(
                available=False,
                exposure=1.0,
                stance="unknown",
                label="无情绪数据",
                note=(
                    f"信号日 {signal_day.isoformat()} 之前没有滞后 "
                    f"{cfg.sentiment_lag_days} 个交易日的情绪因子"
                ),
            )
        stance, label = _stance_of(decision.seal_rate)
        return SentimentView(
            available=True,
            sentiment_date=decision.sentiment_date,
            seal_rate=decision.seal_rate,
            broken_rate=decision.broken_rate,
            max_streak=decision.max_streak,
            limit_up_count=decision.limit_up_count,
            exposure=round(decision.exposure, 4),
            stance=stance,
            label=label,
            note=(
                f"信号日 {signal_day.isoformat()}，采用 "
                f"{decision.sentiment_date.isoformat()} 收盘情绪（滞后 "
                f"{cfg.sentiment_lag_days} 个交易日，避免未来函数）"
            ),
        )


class _MissingFactor(Exception):
    """第 1 段因子算不出来时用于跳出（内部使用）。"""


def _require(value: float | None) -> float:
    """把「应为有效」的可选浮点转成 float；None / NaN / inf 视为缺失。"""
    if value is None or not math.isfinite(value):
        raise _MissingFactor
    return value


def _prescreen_key(item: _Pass1) -> tuple[float, float, float, float]:
    """第 1 段排序键：(趋势结构, 距 20 日线的贴合度, 20 日动量, 成交额)。"""
    trend = (1.0 if item.price >= item.ma20 else 0.0) + (
        1.0 if item.ma20 >= item.ma60 else 0.0
    )
    near_ma20 = -abs(item.price / item.ma20 - 1.0) if item.ma20 > 0 else 0.0
    return (trend, near_ma20, item.momentum_20, item.amount_20)


def _stance_of(seal_rate: float | None) -> tuple[str, str]:
    """按封板率给出情绪档位与中文标签。"""
    if seal_rate is None:
        return "neutral", "情绪中性（缺封板率）"
    if seal_rate >= 0.75:
        return "strong", "情绪强势"
    if seal_rate >= 0.60:
        return "healthy", "情绪健康"
    if seal_rate >= 0.45:
        return "neutral", "情绪中性"
    return "weak", "情绪偏弱"
