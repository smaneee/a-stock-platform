"""买点雷达的样本外验证（walk-forward replay）。

把 :meth:`ScreenerService.run` 的选股规则原样搬到历史上逐日重放，直接复用生产的
``_pass1_factors`` / ``_passes_filters`` / ``_prescreen_key`` / ``_refine``，
而不是重写一份「看起来一样」的规则 —— 重写会掩盖真实差异。

口径
----
- 打分只用 ``<= t`` 的日线，不含未来函数；
- 成交按 ``t+1`` 开盘买入、``t+1+k`` 收盘卖出，扣除双边佣金、印花税与滑点；
- 基准 = 同一天通过同一套硬性过滤的全部标的等权、同规则收益，用来剥离市场 beta；
- 执行约束：次日没有 K 线（停牌 / 退市）不成交，次日开盘价即触及涨停价视为买不到；
- 按等权评估 top_n，不叠加情绪仓位系数（仓位系数是风险预算，不是收益预测）。

已知偏差（会写进报告 ``caveats``，不隐藏）
------------------------------------------
1. 幸存者偏差：本地只有一份「当前」股票池快照，历史上已退市的标的不在样本内；
2. 复权口径：本地有前复权日线时用前复权（除权除息已还原），没有时退回不复权，
   口径随行情库状态变化，报告里会写明本次实际用了哪一种；
3. 窗口重叠：相邻评估日的 k 日收益互相重叠，t 统计量按独立样本读会偏高；
4. 样本区间只有本地日线覆盖的一段，跨风格、跨牛熊代表性不足。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import statistics
import time
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import HistoricalBar
from app.database.session import SessionLocal
from app.market_rules.rules import MarketRuleEngine
from app.realtime.screener import (
    BAR_PERIOD,
    MIN_BARS_FOR_FACTORS,
    PREFERRED_BAR_ADJUST,
    PickView,
    ScreenerConfig,
    ScreenerService,
    ScreenerUnavailable,
    SentimentView,
    SymbolSeries,
    _Pass1,
    _prescreen_key,
    resolve_bar_adjust,
    resolve_bar_adjust_cached,
)
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

# 单次验证的评估窗口上限（交易日）。240 约等于一年，覆盖生产「滚动近一年」的用法；
# 跨多年 walk-forward 由研究脚本显式放宽（param_search.RESEARCH_MAX_EVAL_DAYS）。
MAX_EVAL_DAYS = 240

# 分层检验的分数档位（上界为开区间）
SCORE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("score>=90", 90.0, math.inf),
    ("80<=score<90", 80.0, 90.0),
    ("70<=score<80", 70.0, 80.0),
    ("score<70", -math.inf, 70.0),
)


@dataclass(frozen=True)
class CostModel:
    """交易成本（比例口径）。

    最低 5 元佣金没有单独建模：按 10 万元单票仓位计算，0.03% 佣金 = 30 元 > 5 元下限，
    比例口径不会低估成本。
    """

    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0

    @property
    def buy_cost(self) -> float:
        return self.commission_rate + self.slippage_bps / 10_000.0

    @property
    def sell_cost(self) -> float:
        return self.commission_rate + self.stamp_tax_rate + self.slippage_bps / 10_000.0

    @property
    def round_trip(self) -> float:
        return self.buy_cost + self.sell_cost

    def as_dict(self) -> dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "stamp_tax_rate": self.stamp_tax_rate,
            "slippage_bps": self.slippage_bps,
            "round_trip_pct": round(self.round_trip * 100.0, 4),
        }


@dataclass(frozen=True)
class ValidationConfig:
    """验证参数；选股规则直接复用 ``screener``，避免两份口径漂移。"""

    eval_days: int = 60
    horizons: tuple[int, ...] = (1, 3, 5, 10)
    primary_horizon: int = 3
    costs: CostModel = CostModel()
    screener: ScreenerConfig = ScreenerConfig()
    # none = 按雷达打分选股；random = 同池等量随机样本（噪声底噪）；worst = 同池打分最差的等量样本
    control: str = "none"
    # 评估窗口的右端（含）。None = 取最近 eval_days 个交易日（生产/接口默认）。
    # 指定后窗口整体滑动到该日之前，用于跨年份 walk-forward 样本外。
    end_day: date | None = None

    @property
    def lookback_days(self) -> int:
        return self.screener.lookback_days

    @property
    def top_n(self) -> int:
        return self.screener.top_n

    @property
    def refine_pool(self) -> int:
        return self.screener.refine_pool

    def validate(self, *, max_eval_days: int = MAX_EVAL_DAYS) -> None:
        """校验参数。

        ``max_eval_days`` 默认 240（生产与 API 路径不变）；研究工具做大跨度
        walk-forward 时可以显式放宽，见 ``param_search.RESEARCH_MAX_EVAL_DAYS``。
        """
        self.screener.validate()
        if not 5 <= self.eval_days <= max_eval_days:
            raise ValueError(f"eval_days 必须在 5..{max_eval_days}")
        if not self.horizons:
            raise ValueError("horizons 不能为空")
        if any(h < 1 or h > 60 for h in self.horizons):
            raise ValueError("horizons 每项必须在 1..60")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons 不能重复")
        if self.primary_horizon not in self.horizons:
            raise ValueError("primary_horizon 必须在 horizons 里")
        if self.costs.commission_rate < 0 or self.costs.stamp_tax_rate < 0:
            raise ValueError("成本费率不能为负")
        if self.costs.slippage_bps < 0:
            raise ValueError("滑点不能为负")
        if self.control not in {"none", "random", "worst"}:
            raise ValueError("control 只能是 none / random / worst")


@dataclass(frozen=True)
class HorizonStats:
    """单个持有期（交易日）的样本外统计。"""

    horizon: int
    observations: int
    mean_net_pct: float
    median_net_pct: float
    hit_rate: float
    mean_benchmark_pct: float
    mean_excess_pct: float
    excess_daily_mean_pct: float
    excess_t_stat: float
    excess_days: int
    max_excess_drawdown_pct: float


@dataclass(frozen=True)
class ScoreBucketStats:
    """按命中分数分层的超额收益（主口径持有期）。"""

    label: str
    observations: int
    mean_excess_pct: float
    hit_rate: float


@dataclass(frozen=True)
class DailyRecord:
    """单个评估日的记录（主口径持有期）。"""

    signal_day: date
    eligible: int
    picks: int
    tradable: int
    symbols: tuple[str, ...]
    net_pct: float | None
    benchmark_pct: float | None
    excess_pct: float | None


@dataclass(frozen=True)
class ValidationReport:
    """一次样本外验证的完整结果。"""

    generated_at: str
    first_signal_day: date | None
    last_signal_day: date | None
    evaluation_days: int
    universe_size: int
    bars_adjust: str
    bars_first_day: date | None
    bars_last_day: date | None
    elapsed_seconds: float
    config: ValidationConfig
    horizons: tuple[HorizonStats, ...]
    score_buckets: tuple[ScoreBucketStats, ...]
    daily: tuple[DailyRecord, ...]
    skipped_not_tradable: int
    skipped_no_outcome: int
    control: str
    caveats: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass
class _Bars:
    """一只标的在评估窗口内的日线（升序）+ 对应的全局交易日下标。"""

    symbol: str
    days: list[date]
    day_index: list[int]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    amounts: list[float]

    def position(self, index: int) -> int | None:
        pos = bisect_left(self.day_index, index)
        if pos < len(self.day_index) and self.day_index[pos] == index:
            return pos
        return None


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _t_stat(values: list[float]) -> float:
    """单样本 t 统计量；样本不足或零方差时返回 0。"""
    if len(values) < 2:
        return 0.0
    sd = statistics.stdev(values)
    if sd <= 0:
        return 0.0
    return statistics.fmean(values) / (sd / math.sqrt(len(values)))


def _max_drawdown_pct(daily_returns_pct: list[float]) -> float:
    """按日度收益等权复利累乘后的最大回撤（%，负值）。"""
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in daily_returns_pct:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return worst * 100.0


class RadarValidator:
    """把买点雷达的规则放到历史上逐日重放，产出样本外统计。

    只读本地 ``historical_bars``（前复权优先、缺失回退不复权），不写数据库、不联网。
    """

    def __init__(
        self,
        session_factory: sessionmaker | Callable[[], Session] | None = None,
    ) -> None:
        self._session_factory = session_factory or SessionLocal

    # ─────────── 主流程 ───────────

    def run(
        self,
        config: ValidationConfig | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> ValidationReport:
        """执行验证；本地数据不足时抛 :class:`ScreenerUnavailable`。"""
        cfg = config or ValidationConfig()
        cfg.validate()
        started = time.perf_counter()
        notes: list[str] = []

        with self._session_factory() as db:
            meta_map, snapshot_day = ScreenerService._load_universe(db)
            if not meta_map:
                raise ScreenerUnavailable(
                    "本地股票池为空，请先调用 POST /api/universe/sync"
                )
            bars, bars_adjust = self._load_bars(db, meta_map, cfg)

        if not bars:
            raise ScreenerUnavailable("本地没有可用的日线，无法做样本外验证")
        days = self._trading_days(bars)
        eval_indices = self._eval_indices(len(days), cfg)
        if not eval_indices:
            raise ScreenerUnavailable(
                "本地日线不足以覆盖 lookback + 评估窗口，无法做样本外验证"
            )

        rules = MarketRuleEngine()
        scfg = replace(cfg.screener, scale_exposure_by_sentiment=False)
        neutral = SentimentView(
            available=False,
            exposure=1.0,
            stance="neutral",
            label="验证：等权",
            note="样本外验证按等权评估，不叠加情绪仓位系数",
        )

        pooled_picks: dict[int, list[float]] = {k: [] for k in cfg.horizons}
        pooled_bench: dict[int, list[float]] = {k: [] for k in cfg.horizons}
        daily_excess: dict[int, list[float]] = {k: [] for k in cfg.horizons}
        bucket_values: dict[str, list[float]] = {label: [] for label, _, _ in SCORE_BUCKETS}
        daily_records: list[DailyRecord] = []
        skipped_not_tradable = 0
        skipped_no_outcome = 0

        primary = cfg.primary_horizon
        total_days = len(eval_indices)
        for done, index in enumerate(eval_indices, start=1):
            items = self._cross_section(bars, index, meta_map, scfg)
            items.sort(key=_prescreen_key, reverse=True)
            pool = items[: scfg.refine_pool]

            # 基准先算：同一天、同一套硬性过滤的全部标的等权、同规则收益
            bench_values: dict[int, list[float]] = {k: [] for k in cfg.horizons}
            for item in items:
                values, _, _ = self._outcome(
                    bars[item.symbol], index, cfg, rules, None, meta_map
                )
                for k, value in values.items():
                    if value is not None:
                        bench_values[k].append(value)
            bench_primary = _mean(bench_values[primary])

            selections: list[tuple[str, float, dict[int, float | None], int, int]] = []
            if cfg.control == "none":
                refined: list[PickView] = []
                for item in pool:
                    pick = ScreenerService._refine(item, rules, scfg, neutral)
                    if pick is not None:
                        refined.append(pick)
                refined.sort(key=lambda p: (-p.score, -p.amount_20, p.symbol))
                for pick in refined[: scfg.top_n]:
                    values, blocked, missing = self._outcome(
                        bars[pick.symbol], index, cfg, rules, pick, meta_map
                    )
                    selections.append((pick.symbol, pick.score, values, blocked, missing))
            else:
                # 对照组：不按分数选，用同一套成交与成本规则算收益
                chosen = (
                    self._control_sample(items, days[index], scfg.top_n)
                    if cfg.control == "random"
                    else items[-scfg.top_n :]
                )
                for item in chosen:
                    values, blocked, missing = self._outcome(
                        bars[item.symbol], index, cfg, rules, None, meta_map
                    )
                    selections.append((item.symbol, item.amount_20, values, blocked, missing))

            pick_values: dict[int, list[float]] = {k: [] for k in cfg.horizons}
            for _symbol, score, values, blocked, missing in selections:
                skipped_not_tradable += blocked
                skipped_no_outcome += missing
                for k, value in values.items():
                    if value is not None:
                        pick_values[k].append(value)
                primary_value = values[primary]
                if cfg.control != "none" or primary_value is None or not bench_values[primary]:
                    continue
                excess = primary_value - bench_primary
                for label, low, high in SCORE_BUCKETS:
                    if low <= score < high:
                        bucket_values[label].append(excess)
                        break

            for k in cfg.horizons:
                pooled_picks[k].extend(pick_values[k])
                pooled_bench[k].extend(bench_values[k])
                if pick_values[k] and bench_values[k]:
                    daily_excess[k].append(
                        _mean(pick_values[k]) - _mean(bench_values[k])
                    )

            net_primary = _mean(pick_values[primary]) if pick_values[primary] else None
            daily_records.append(
                DailyRecord(
                    signal_day=days[index],
                    eligible=len(items),
                    picks=len(selections),
                    tradable=len(pick_values[primary]),
                    symbols=tuple(symbol for symbol, *_ in selections),
                    net_pct=None if net_primary is None else round(net_primary, 3),
                    benchmark_pct=(
                        round(bench_primary, 3) if bench_values[primary] else None
                    ),
                    excess_pct=(
                        round(net_primary - bench_primary, 3)
                        if net_primary is not None and bench_values[primary]
                        else None
                    ),
                )
            )
            if progress is not None:
                progress(done, total_days)

        horizon_stats = tuple(
            self._horizon_stats(k, pooled_picks[k], pooled_bench[k], daily_excess[k])
            for k in cfg.horizons
        )
        buckets = () if cfg.control != "none" else tuple(
            ScoreBucketStats(
                label=label,
                observations=len(bucket_values[label]),
                mean_excess_pct=round(_mean(bucket_values[label]), 3),
                hit_rate=(
                    round(
                        sum(1 for v in bucket_values[label] if v > 0)
                        / len(bucket_values[label]),
                        4,
                    )
                    if bucket_values[label]
                    else 0.0
                ),
            )
            for label, _, _ in SCORE_BUCKETS
        )

        first_day = days[eval_indices[0]]
        last_day = days[eval_indices[-1]]
        eligible_mean = _mean([float(record.eligible) for record in daily_records])
        notes.append(
            f"评估日 {first_day.isoformat()} ~ {last_day.isoformat()}，共 {len(eval_indices)} 天，"
            f"每日等权买入 top_n={scfg.top_n}，持有 "
            + "/".join(str(k) for k in cfg.horizons)
            + " 个交易日"
        )
        notes.append(
            f"基准 = 当日通过同一套硬性过滤的全部标的（日均 {eligible_mean:.0f} 只）等权、同规则收益"
        )
        notes.append(
            "成交假设：次日开盘买入、持有到收盘卖出；次日停牌或开盘即涨停计为买不到"
            f"（本次 {skipped_not_tradable} 次）"
        )
        notes.append(
            f"成本：佣金 {cfg.costs.commission_rate * 100:.3f}% 双边 + 印花税 "
            f"{cfg.costs.stamp_tax_rate * 100:.3f}%（仅卖出）+ 滑点 "
            f"{cfg.costs.slippage_bps:g}bp 单边，往返合计 {cfg.costs.round_trip * 100:.3f}%"
        )
        if cfg.control == "random":
            notes.append(
                "对照模式 random：不按雷达打分，改用同池等量确定性随机样本，用于衡量噪声底噪"
                "（其超额收益应接近 0；若明显偏离，说明验证脚手架本身可疑）"
            )
        elif cfg.control == "worst":
            notes.append("对照模式 worst：取同池打分最差的同样数量标的，用于检查分数是否具备区分度")
        notes.append(
            f"复权口径：{bars_adjust}"
            + ("（前复权，已用通达信除权除息数据在本地还原）" if bars_adjust == PREFERRED_BAR_ADJUST else "（不复权，未做除权除息还原）")
        )
        notes.append("分析结果仅用于研究，不构成投资建议")

        if bars_adjust == PREFERRED_BAR_ADJUST:
            adjust_caveat = (
                f"前复权口径（{bars_adjust}）：日线已用通达信除权除息数据在本地还原复权价，"
                "持仓窗口内的分红 / 送转不会被算成下跌；但复权价按最新交易日归一，"
                "与账户里的实际成本口径不同。"
            )
        else:
            adjust_caveat = (
                "不复权口径：本地还没有前复权日线（adjust=qfq），持仓窗口内发生除权除息时，"
                "分红 / 送转会表现为价格下跌，会低估真实收益。"
            )
        caveats = (
            f"幸存者偏差：本地只有一份「当前」股票池快照（{snapshot_day.isoformat()}），"
            "历史上已退市 / 已剔除的标的不在样本内，会系统性高估收益。",
            adjust_caveat,
            "窗口重叠：相邻评估日的 k 日收益互相重叠，t 统计量按独立样本读会偏高，只能当参考。",
            "成交假设偏乐观：按开盘价全额成交，未建模涨跌停排队、集合竞价滑点与流动性冲击。",
            "样本区间只有本地日线覆盖的一段，跨风格、跨牛熊代表性不足。",
            "未做多重检验校正：触发器与权重是人工设定的，本报告只说明「给定规则在这段历史上的表现」，"
            "不能证明它在未来仍然有效。",
        )

        return ValidationReport(
            generated_at=utc_now().isoformat(),
            first_signal_day=first_day,
            last_signal_day=last_day,
            evaluation_days=len(eval_indices),
            universe_size=len(meta_map),
            bars_adjust=bars_adjust,
            bars_first_day=days[0],
            bars_last_day=days[-1],
            elapsed_seconds=round(time.perf_counter() - started, 3),
            config=cfg,
            horizons=horizon_stats,
            score_buckets=buckets,
            daily=tuple(daily_records),
            skipped_not_tradable=skipped_not_tradable,
            skipped_no_outcome=skipped_no_outcome,
            control=cfg.control,
            caveats=caveats,
            notes=tuple(notes),
        )

    # ─────────── 数据装载 ───────────

    def _load_bars(
        self,
        db: Session,
        meta_map: dict[str, dict[str, object]],
        cfg: ValidationConfig,
    ) -> tuple[dict[str, _Bars], str]:
        """读入评估窗口内的日线；窗口 = lookback + 评估天数 + 最长持有期。

        复权口径与生产选股保持一致（前复权优先、缺失自动回退），返回
        ``(bars, 实际口径)``。
        """
        # 用共享的 TTL 版本：直连 `resolve_bar_adjust` 每次都要付一次
        # 13,359,225 行的 `GROUP BY adjust`（实测 p50 6.06 秒）。
        adjust, _max_id, _last_day = resolve_bar_adjust_cached(db, BAR_PERIOD)
        need = cfg.lookback_days + cfg.eval_days + max(cfg.horizons) + 2
        window = [
            HistoricalBar.period == BAR_PERIOD,
            HistoricalBar.adjust == adjust,
        ]
        # end_day 非空 = 把评估窗口整体滑动到该日（含）之前，用于跨年份 walk-forward；
        # 为空时行为与原来完全一致（取最近 need 个交易日）。
        if cfg.end_day is not None:
            window.append(HistoricalBar.trade_date <= cfg.end_day)
        recent = (
            db.execute(
                select(HistoricalBar.trade_date)
                .where(*window)
                .group_by(HistoricalBar.trade_date)
                .order_by(HistoricalBar.trade_date.desc())
                .limit(need)
            )
            .scalars()
            .all()
        )
        if not recent:
            return {}, adjust
        floor = min(recent)
        rows = db.execute(
            select(
                HistoricalBar.symbol,
                HistoricalBar.trade_date,
                HistoricalBar.open,
                HistoricalBar.high,
                HistoricalBar.low,
                HistoricalBar.close,
                HistoricalBar.volume,
                HistoricalBar.amount,
            )
            .where(*window, HistoricalBar.trade_date >= floor)
            .order_by(HistoricalBar.symbol, HistoricalBar.trade_date)
        ).all()

        bars: dict[str, _Bars] = {}
        for symbol, trade_date, open_, high, low, close, volume, amount in rows:
            if symbol not in meta_map:
                continue
            bucket = bars.get(symbol)
            if bucket is None:
                bucket = _Bars(
                    symbol=symbol,
                    days=[],
                    day_index=[],
                    opens=[],
                    highs=[],
                    lows=[],
                    closes=[],
                    volumes=[],
                    amounts=[],
                )
                bars[symbol] = bucket
            bucket.days.append(trade_date)
            bucket.opens.append(float(open_ or 0.0))
            bucket.highs.append(float(high or 0.0))
            bucket.lows.append(float(low or 0.0))
            bucket.closes.append(float(close or 0.0))
            bucket.volumes.append(float(volume or 0.0))
            bucket.amounts.append(float(amount or 0.0))
        return bars, adjust

    @staticmethod
    def _trading_days(bars: dict[str, _Bars]) -> list[date]:
        """评估窗口内出现过的全部交易日（升序），并回填每根 K 线的全局下标。"""
        days = sorted({day for bucket in bars.values() for day in bucket.days})
        index_of = {day: i for i, day in enumerate(days)}
        for bucket in bars.values():
            bucket.day_index = [index_of[day] for day in bucket.days]
        return days

    @staticmethod
    def _control_sample(items: list[_Pass1], day: date, count: int) -> list[_Pass1]:
        """对照组：按 ``blake2b(symbol:day)`` 确定性抽样，保证可复现。"""
        ordered = sorted(
            items,
            key=lambda item: hashlib.blake2b(
                f"{item.symbol}:{day.isoformat()}".encode("utf-8"), digest_size=8
            ).hexdigest(),
        )
        return ordered[:count]

    @staticmethod
    def _eval_indices(days_len: int, cfg: ValidationConfig) -> list[int]:
        """可评估的信号日下标：前面留足 lookback，后面留足最长持有期。"""
        last = days_len - 2 - max(cfg.horizons)
        first = max(MIN_BARS_FOR_FACTORS - 1, last - cfg.eval_days + 1)
        if last < first:
            return []
        return list(range(first, last + 1))

    # ─────────── 单日横截面 ───────────

    @staticmethod
    def _cross_section(
        bars: dict[str, _Bars],
        index: int,
        meta_map: dict[str, dict[str, object]],
        cfg: ScreenerConfig,
    ) -> list[_Pass1]:
        """重放第一段：基础因子 + 硬性过滤（只用 <= 该日的日线）。"""
        items: list[_Pass1] = []
        lookback = cfg.lookback_days
        for symbol, meta in meta_map.items():
            bucket = bars.get(symbol)
            if bucket is None:
                continue
            pos = bucket.position(index)
            if pos is None:
                continue
            start = max(0, pos - lookback + 1)
            if pos - start + 1 < MIN_BARS_FOR_FACTORS:
                continue
            series = SymbolSeries(
                symbol=symbol,
                days=tuple(bucket.days[start : pos + 1]),
                opens=tuple(bucket.opens[start : pos + 1]),
                highs=tuple(bucket.highs[start : pos + 1]),
                lows=tuple(bucket.lows[start : pos + 1]),
                closes=tuple(bucket.closes[start : pos + 1]),
                volumes=tuple(bucket.volumes[start : pos + 1]),
                amounts=tuple(bucket.amounts[start : pos + 1]),
            )
            item = ScreenerService._pass1_factors(symbol, meta, series, None, False)
            if item is None:
                continue
            if not ScreenerService._passes_filters(item, cfg):
                continue
            items.append(item)
        return items

    def _outcome(
        self,
        bucket: _Bars,
        index: int,
        cfg: ValidationConfig,
        rules: MarketRuleEngine,
        pick: PickView | None,
        meta_map: dict[str, dict[str, object]],
    ) -> tuple[dict[int, float | None], int, int]:
        """算各持有期的净收益；返回 (收益, 买不到次数, 数据不足次数)。

        "数据不足" 只在所有持有期都算不出来时记 1，避免按持有期重复计数。
        """
        values: dict[int, float | None] = {k: None for k in cfg.horizons}
        pos = bucket.position(index)
        if pos is None or pos + 1 >= len(bucket.closes):
            return values, 0, 1
        entry_pos = pos + 1
        if bucket.day_index[entry_pos] != index + 1:
            # 次日该标的没有 K 线：停牌 / 已退市，买不到
            return values, 1, 0
        if bucket.volumes[entry_pos] <= 0:
            return values, 1, 0
        if self._opened_limit_up(bucket, pos, entry_pos, rules, meta_map, pick):
            return values, 1, 0
        for k in cfg.horizons:
            exit_pos = entry_pos + k
            if exit_pos >= len(bucket.closes) or bucket.day_index[exit_pos] != index + 1 + k:
                continue
            value = self._net_return(bucket, entry_pos, exit_pos, cfg.costs)
            if value is not None:
                values[k] = value
        if all(value is None for value in values.values()):
            return values, 0, 1
        return values, 0, 0

    @staticmethod
    def _opened_limit_up(
        bucket: _Bars,
        pos: int,
        entry_pos: int,
        rules: MarketRuleEngine,
        meta_map: dict[str, dict[str, object]],
        pick: PickView | None,
    ) -> bool:
        """次日开盘即触及涨停价 → 按买不到处理（保守）。"""
        previous_close = bucket.closes[pos]
        if previous_close <= 0:
            return False
        meta = meta_map.get(bucket.symbol, {})
        name = pick.name if pick is not None else str(meta.get("name") or "")
        limit_up = rules.get_rules(
            bucket.symbol, name, bool(meta.get("is_st"))
        ).limit_up(Decimal(str(previous_close)))
        if limit_up is None:
            return False
        return Decimal(str(bucket.opens[entry_pos])) >= limit_up

    @staticmethod
    def _net_return(
        bucket: _Bars, entry_pos: int, exit_pos: int, costs: CostModel
    ) -> float | None:
        entry = bucket.opens[entry_pos]
        exit_price = bucket.closes[exit_pos]
        if entry <= 0 or exit_price <= 0:
            return None
        gross = exit_price / entry
        net = (gross * (1.0 - costs.sell_cost)) / (1.0 + costs.buy_cost) - 1.0
        return net * 100.0

    # ─────────── 统计 ───────────

    @staticmethod
    def _horizon_stats(
        horizon: int,
        picks: list[float],
        bench: list[float],
        daily: list[float],
    ) -> HorizonStats:
        observations = len(picks)
        mean_net = _mean(picks)
        return HorizonStats(
            horizon=horizon,
            observations=observations,
            mean_net_pct=round(mean_net, 3),
            median_net_pct=round(_median(picks), 3),
            hit_rate=(
                round(sum(1 for v in picks if v > 0) / observations, 4)
                if observations
                else 0.0
            ),
            mean_benchmark_pct=round(_mean(bench), 3),
            mean_excess_pct=round(mean_net - _mean(bench), 3),
            excess_daily_mean_pct=round(_mean(daily), 3),
            excess_t_stat=round(_t_stat(daily), 2),
            excess_days=len(daily),
            max_excess_drawdown_pct=round(_max_drawdown_pct(daily), 2),
        )


class RadarValidationService:
    """验证任务的进程内调度：同一时刻只跑一个，缓存最后一次报告。

    报告不落库（验证是可重算的研究动作），重启后需要重新运行；这样避免为一次性
    研究结论引入数据库迁移。单次 60 个评估日实测约 100 秒，因此走后台任务 +
    状态轮询，而不是阻塞 HTTP 请求。
    """

    def __init__(
        self,
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        validator: RadarValidator | None = None,
    ) -> None:
        self._validator = validator or RadarValidator(session_factory)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state = "idle"  # idle / running / done / failed
        self._done = 0
        self._total = 0
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None
        self._report: ValidationReport | None = None

    # ─────────── 查询 ───────────

    @property
    def report(self) -> ValidationReport | None:
        return self._report

    def status(self) -> dict[str, object]:
        """当前任务状态（前端轮询用）。"""
        return {
            "state": self._state,
            "progress": {"done": self._done, "total": self._total},
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "error": self._error,
            "has_report": self._report is not None,
            "report_generated_at": (
                self._report.generated_at if self._report is not None else None
            ),
        }

    # ─────────── 生命周期 ───────────

    async def start(self, config: ValidationConfig) -> dict[str, object]:
        """启动验证；已在运行则直接返回当前状态（幂等）。"""
        async with self._lock:
            if self._state == "running":
                return self.status()
            self._state = "running"
            self._done = 0
            self._total = config.eval_days
            self._error = None
            self._started_at = utc_now().isoformat()
            self._finished_at = None
            self._task = asyncio.create_task(
                self._run(config), name="radar-validation"
            )
        return self.status()

    async def _run(self, config: ValidationConfig) -> None:
        try:
            report = await asyncio.to_thread(
                self._validator.run, config, self._on_progress
            )
        except Exception as exc:  # noqa: BLE001 - 失败要写进状态而不是拖垮服务
            self._state = "failed"
            self._error = f"{type(exc).__name__}: {exc}"
            logger.exception("买点雷达样本外验证失败")
        else:
            self._report = report
            self._state = "done"
        finally:
            self._finished_at = utc_now().isoformat()

    def _on_progress(self, done: int, total: int) -> None:
        self._done, self._total = done, total

    async def wait(self) -> ValidationReport | None:
        """等待当前任务结束（测试与脚本用）。"""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self._report

    async def close(self) -> None:
        """应用关停时取消未完成的验证任务。"""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
