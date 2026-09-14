"""雷达参数寻优引擎：训练 / 留出切分下的触发器权重与阈值搜索（研究工具）。

动机
----
雷达的 8 个触发器权重是人工设定的。``validation.py`` 只能验证**当前**那一套权重，
没法回答「换个权重 / 阈值能不能跑赢」。这里把同一套规则放到历史上逐日重放，先在
训练段搜索权重与阈值，再在完全隔离的留出段上**只跑一次**，用来判断训练段挑出来的
方案是不是过拟合噪声。

口径与生产一致
--------------
分数 = ``sum(TRIGGER_WEIGHTS[k] for k in triggers)``，排序键 =
``(-score, -amount_20, symbol)``（与 ``ScreenerService.run`` 相同）；成交按次日开盘
买入、``t+1+k`` 收盘卖出并扣成本；基准 = 当日全部通过硬性过滤的标的等权同规则收益。

实现上不重写规则，而是直接复用 ``RadarValidator._cross_section`` /
``RadarValidator._outcome`` / ``ScreenerService._refine``，只把昂贵部分缓存一次：
之后任意权重 / ``min_triggers`` / ``top_n`` 组合都能在缓存上瞬间重算。

已知偏差（与 ``validation.py`` 一致，不掩盖）
--------------------------------------------
幸存者偏差、窗口重叠（t 值偏高）、样本只覆盖本地日线一段、网格搜索本身的多重检验。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from datetime import date

import numpy as np

from app.market_rules.rules import MarketRuleEngine
from app.realtime.screener import (
    TRIGGER_WEIGHTS,
    ScreenerService,
    SentimentView,
    _prescreen_key,
)
from app.realtime.validation import RadarValidator, ValidationConfig, _t_stat

KEYS: tuple[str, ...] = tuple(TRIGGER_WEIGHTS)
KEY_IX: dict[str, int] = {k: i for i, k in enumerate(KEYS)}
HORIZONS: tuple[int, ...] = (1, 3, 5, 10)
H_PRIMARY = 3  # 主口径持有期（交易日），与 ValidationConfig.primary_horizon 一致
PRIMARY_IX = HORIZONS.index(H_PRIMARY)
CONTROL_SALTS = 30  # 噪声带用的随机对照盐值个数
# 研究工具允许的最大评估窗口：5.5 年约 1330 个交易日，取 1400 留余量。
# 只影响 build_cache 的显式校验，生产/API 路径仍走 validation.MAX_EVAL_DAYS=240。
RESEARCH_MAX_EVAL_DAYS = 1400
MIN_GROUP = 5  # 因子诊断里命中 / 未命中任一组少于该数量的交易日直接跳过
# 在候选池里几乎恒为真（命中率≈1）的条件：池子本身已按趋势结构排序，它们没有区分度
DEGENERATE_IN_POOL = ("above_ma20", "ma20_above_ma60", "rsi_rebound")


# ───────────────────────────── 纯函数 ─────────────────────────────


def score_of(triggers, weights: dict[str, float]) -> float:
    """命中触发器集合的加权和（与 ``ScreenerService._refine`` 同一公式）。"""
    return round(sum(weights.get(k, 0.0) for k in triggers), 2)


def rank_pool(onehot, counts, amounts, symbols, weights, min_triggers, top_n):
    """按生产口径挑出 ``top_n``：先按命中个数过滤，再按 (-score, -amount_20, symbol)。

    ``symbols`` 用整型 id（比字符串快很多）。只有在 score 与 amount_20 同时完全相等时，
    并列顺序才可能与生产按字符串排序的结果不同；实测候选池里不出现这种并列。

    返回命中行在候选池里的下标。
    """
    w = np.asarray([weights.get(k, 0.0) for k in KEYS], dtype=np.float64)
    score = onehot @ w
    cand = np.nonzero(counts >= min_triggers)[0]
    if cand.size == 0:
        return np.empty(0, dtype=np.int64)
    order = np.lexsort((symbols[cand], -amounts[cand], -score[cand]))
    return cand[order][:top_n]


# ───────────────────────────── 缓存构建 ─────────────────────────────


def build_cache(eval_days: int = 240, log=print, end_day: date | None = None) -> dict:
    """逐日重放一次，把入选池、各持有期收益、触发器命中全部缓存下来。

    ``end_day`` 不为空时，评估窗口整体滑动到该日（含）之前，用于跨年份
    walk-forward；为空时取最近 ``eval_days`` 个交易日。
    """
    cfg = ValidationConfig(
        eval_days=eval_days,
        horizons=HORIZONS,
        primary_horizon=H_PRIMARY,
        end_day=end_day,
    )
    cfg.validate(max_eval_days=RESEARCH_MAX_EVAL_DAYS)
    validator = RadarValidator()
    started = time.perf_counter()

    with validator._session_factory() as db:  # noqa: SLF001
        meta_map, snapshot_day = ScreenerService._load_universe(db)  # noqa: SLF001
        bars, adjust = validator._load_bars(db, meta_map, cfg)  # noqa: SLF001
    log(
        f"股票池 {len(meta_map)} 只，可用日线 {len(bars)} 只，复权口径 {adjust}，"
        f"装载耗时 {time.perf_counter() - started:.1f}s"
    )
    if not bars:
        raise RuntimeError("本地没有可用日线，无法做参数寻优")

    days = validator._trading_days(bars)  # noqa: SLF001
    eval_indices = validator._eval_indices(len(days), cfg)  # noqa: SLF001
    if not eval_indices:
        raise RuntimeError("本地日线不足以覆盖 lookback + 评估窗口，无法做参数寻优")
    log(
        f"交易日 {len(days)} 天（{days[0]} ~ {days[-1]}），可评估 {len(eval_indices)} 天"
        f"（{days[eval_indices[0]]} ~ {days[eval_indices[-1]]}）"
    )

    rules = MarketRuleEngine()
    scfg = replace(cfg.screener, scale_exposure_by_sentiment=False)
    refine_cfg = replace(scfg, min_triggers=1)  # 打开到 1，才能拿到完整触发器集合
    neutral = SentimentView(
        available=False, exposure=1.0, stance="neutral", label="验证：等权", note=""
    )

    symbol_ids: dict[str, int] = {}
    records: list[dict] = []
    for done, index in enumerate(eval_indices, start=1):
        items = validator._cross_section(bars, index, meta_map, scfg)  # noqa: SLF001
        items.sort(key=_prescreen_key, reverse=True)

        sym_arr = np.empty(len(items), dtype=np.int32)
        val_arr = np.full((len(items), len(HORIZONS)), np.nan, dtype=np.float32)
        bench_sum = [0.0] * len(HORIZONS)
        bench_cnt = [0] * len(HORIZONS)
        for i, item in enumerate(items):
            sym_arr[i] = symbol_ids.setdefault(item.symbol, len(symbol_ids))
            values, _blocked, _missing = validator._outcome(  # noqa: SLF001
                bars[item.symbol], index, cfg, rules, None, meta_map
            )
            for h_i, horizon in enumerate(HORIZONS):
                value = values[horizon]
                if value is not None:
                    val_arr[i, h_i] = value
                    bench_sum[h_i] += value
                    bench_cnt[h_i] += 1

        pool_trig: list[tuple[str, ...] | None] = []
        pool_amt: list[float] = []
        for item in items[: scfg.refine_pool]:
            pick = ScreenerService._refine(item, rules, refine_cfg, neutral)  # noqa: SLF001
            pool_trig.append(None if pick is None else tuple(pick.triggers))
            pool_amt.append(0.0 if pick is None else float(pick.amount_20))

        records.append(
            {
                "date": days[index].isoformat(),
                "sym": sym_arr,
                "val": val_arr,
                "pool_trig": pool_trig,
                "pool_amt": np.asarray(pool_amt, dtype=np.float32),
                "bench_sum": bench_sum,
                "bench_cnt": bench_cnt,
            }
        )
        if done % 20 == 0 or done == len(eval_indices):
            log(
                f"  重放 {done}/{len(eval_indices)}（{days[index]}）"
                f" 累计 {time.perf_counter() - started:.0f}s"
            )

    symbols: list[str] = [""] * len(symbol_ids)
    for symbol, sid in symbol_ids.items():
        symbols[sid] = symbol
    return {
        "symbols": symbols,
        "days": records,
        "adjust": adjust,
        "snapshot_day": snapshot_day.isoformat(),
        "universe": len(meta_map),
        "eval_days": eval_days,
        "end_day": None if end_day is None else end_day.isoformat(),
    }


# ───────────────────────────── 缓存视图与评估 ─────────────────────────────


def build_folds(
    dates_len: int, train: int, gap: int, hold: int, step: int, max_folds: int
) -> list[tuple[int, int]]:
    """从最新一天向前滚动切折，返回 ``[(lo, hi)]``（闭区间下标）。

    每折长度固定为 ``train + gap + hold``；折与折之间按 ``step`` 回退，因此
    训练段会随折数增加而向前滑动（真正的 walk-forward，不是固定切分）。
    ``hi`` 始终是**更靠后**的那一段（留出段），训练段在它前面。
    """
    if train <= 0 or hold <= 0 or gap < 0 or step <= 0 or max_folds <= 0:
        raise ValueError("build_folds 参数非法：train/hold/step/max_folds 必须为正，gap 不能为负")
    size = train + gap + hold
    folds: list[tuple[int, int]] = []
    end = dates_len - 1
    while end >= 0 and len(folds) < max_folds:
        lo = end - size + 1
        if lo < 0:
            break
        folds.append((lo, end))
        end -= step
    return folds


def slice_payload(payload: dict, lo: int, hi: int) -> dict:
    """按闭区间截取缓存里的评估日；其它字段（symbols / adjust / 快照日）原样保留。"""
    days = payload["days"][lo : hi + 1]
    if not days:
        raise ValueError(f"切片为空：lo={lo} hi={hi}（缓存共 {len(payload['days'])} 天）")
    return {**payload, "days": days}


def noise_stats(values: list[float]) -> dict:
    """随机对照（各盐值）t 值的分布；没有样本时全为 None，避免误读成 0。"""
    if not values:
        return {"mean": None, "sd": None, "min": None, "max": None, "n": 0}
    return {
        "mean": round(float(np.mean(values)), 2),
        "sd": round(float(np.std(values)), 2),
        "min": round(float(min(values)), 2),
        "max": round(float(max(values)), 2),
        "n": len(values),
    }


class Book:
    """把缓存整理成可直接网格搜索的结构（每日候选池的 one-hot、收益、基准）。"""

    def __init__(self, payload: dict) -> None:
        self._raw_days: list[dict] = payload["days"]
        self.symbols: list[str] = payload["symbols"]
        self.adjust: str = payload["adjust"]
        self.universe: int = int(payload.get("universe", len(self.symbols)))
        self.snapshot_day: str = str(payload.get("snapshot_day", ""))
        self.dates: list[str] = []
        self.sym: list[np.ndarray] = []
        self.val: list[np.ndarray] = []
        self.onehot: list[np.ndarray] = []
        self.counts: list[np.ndarray] = []
        self.amounts: list[np.ndarray] = []
        self.bench: list[list[np.ndarray]] = []
        self.bench_mean: list[np.ndarray] = []
        self.rand_order: list[np.ndarray] = []
        for rec in self._raw_days:
            self.dates.append(rec["date"])
            self.sym.append(rec["sym"])
            self.val.append(rec["val"])
            onehot = np.zeros((len(rec["pool_trig"]), len(KEYS)), dtype=np.float64)
            for i, triggers in enumerate(rec["pool_trig"]):
                for key in triggers or ():
                    onehot[i, KEY_IX[key]] = 1.0
            self.onehot.append(onehot)
            self.counts.append(onehot.sum(axis=1))
            self.amounts.append(np.asarray(rec["pool_amt"], dtype=np.float64))
            columns, means = [], []
            for h_i in range(len(HORIZONS)):
                column = rec["val"][:, h_i]
                columns.append(column[~np.isnan(column)])
                seen = rec["bench_cnt"][h_i]
                means.append(
                    float(rec["bench_sum"][h_i] / seen) if seen else float("nan")
                )
            self.bench.append(columns)
            self.bench_mean.append(np.asarray(means, dtype=np.float64))
            self.rand_order.append(self._order_for(rec, 0))

    def __len__(self) -> int:
        return len(self.dates)

    def _order_for(self, rec: dict, salt: int) -> np.ndarray:
        """随机对照排序：``blake2b(salt:symbol:date)`` 升序（salt=0 时与生产一致）。"""
        digest = np.empty(len(rec["sym"]), dtype=np.int64)
        prefix = f"{salt}:" if salt else ""
        for i, sid in enumerate(rec["sym"]):
            raw = f"{prefix}{self.symbols[int(sid)]}:{rec['date']}"
            digest[i] = int.from_bytes(
                hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest(),
                "big",
                signed=True,
            )
        return np.argsort(digest, kind="stable")

    def resalt(self, salt: int) -> None:
        """只重算随机对照排序，用于测「同一套脚手架下的噪声带」。"""
        self.rand_order = [self._order_for(rec, salt) for rec in self._raw_days]

    def pooled_bench_for(self, idx_list: list[int]) -> list[list[float]]:
        """给定评估日子集，把基准收益按持有期拼成列表（供 ``_horizon_stats`` 用）。"""
        return [
            [v for i in idx_list for v in self.bench[i][h].tolist()]
            for h in range(len(HORIZONS))
        ]


def evaluate(
    book: Book,
    idx_list: list[int],
    bench_pooled: list[list[float]],
    kind: str,
    weights: dict[str, float],
    min_triggers: int,
    top_n: int,
) -> dict:
    """按给定方案重放评估日，返回与生产 ``_horizon_stats`` 同口径的统计。"""
    picks: list[list[float]] = [[] for _ in HORIZONS]
    daily: list[list[float]] = [[] for _ in HORIZONS]
    pick_count = 0
    for i in idx_list:
        if kind == "random":
            values = book.val[i][book.rand_order[i][:top_n]]
        elif kind == "worst":
            end = len(book.sym[i])
            values = book.val[i][np.arange(end - top_n, end)]
        else:
            take = rank_pool(
                book.onehot[i], book.counts[i], book.amounts[i], book.sym[i],
                weights, min_triggers, top_n,
            )
            values = book.val[i][take]
        pick_count += len(values)
        for h_i in range(len(HORIZONS)):
            column = values[:, h_i]
            good = column[~np.isnan(column)]
            if not good.size:
                continue
            picks[h_i].extend(good.tolist())
            bench = book.bench_mean[i][h_i]
            if not np.isnan(bench) and book.bench[i][h_i].size:
                daily[h_i].append(float(np.mean(good)) - float(bench))
    stats: dict = {"_picks": pick_count}
    for h_i, horizon in enumerate(HORIZONS):
        stats[horizon] = RadarValidator._horizon_stats(  # noqa: SLF001
            horizon, picks[h_i], bench_pooled[h_i], daily[h_i]
        )
    return stats


# ───────────────────────────── 因子诊断 ─────────────────────────────


def hit_rate(book: Book, idx_list: list[int], key: str) -> float:
    """候选池内该条件的命中率；接近 1 说明它在池里几乎恒为真（无区分度）。"""
    hit = total = 0
    for i in idx_list:
        valid = book.counts[i] >= 1
        hit += int((book.onehot[i][valid, KEY_IX[key]] > 0).sum())
        total += int(valid.sum())
    return hit / total if total else 0.0


def factor_stats(book: Book, idx_list: list[int], key: str, min_group: int = MIN_GROUP) -> dict:
    """候选池内「命中 vs 未命中」的主口径超额差（用逐日序列算 t 值）。"""
    daily: list[float] = []
    hit_all: list[float] = []
    miss_all: list[float] = []
    for i in idx_list:
        valid = book.counts[i] >= 1
        column = book.onehot[i][:, KEY_IX[key]] > 0
        hit = np.nonzero(valid & column)[0]
        miss = np.nonzero(valid & ~column)[0]
        if hit.size < min_group or miss.size < min_group:
            continue
        vals_hit = book.val[i][hit, PRIMARY_IX]
        vals_miss = book.val[i][miss, PRIMARY_IX]
        vals_hit = vals_hit[~np.isnan(vals_hit)]
        vals_miss = vals_miss[~np.isnan(vals_miss)]
        if not vals_hit.size or not vals_miss.size:
            continue
        hit_all.extend(vals_hit.tolist())
        miss_all.extend(vals_miss.tolist())
        daily.append(float(np.mean(vals_hit) - np.mean(vals_miss)))
    return {
        "days": len(daily),
        "n_hit": len(hit_all),
        "n_miss": len(miss_all),
        "hit_mean_pct": round(float(np.mean(hit_all)) if hit_all else 0.0, 3),
        "miss_mean_pct": round(float(np.mean(miss_all)) if miss_all else 0.0, 3),
        "spread_pct": round(float(np.mean(daily)) if daily else 0.0, 3),
        "t": round(_t_stat(daily), 2),
    }


def top_bottom_spread(
    book: Book, idx_list: list[int], weights: dict[str, float],
    top_n: int = 10, min_group: int = MIN_GROUP,
) -> dict:
    """同一候选池内「打分最高 top_n 只 − 最低 top_n 只」的主口径价差。"""
    daily: list[float] = []
    for i in idx_list:
        size = int((book.counts[i] >= 1).sum())
        if size < 2 * min_group:
            continue
        take = rank_pool(
            book.onehot[i], book.counts[i], book.amounts[i], book.sym[i],
            weights, 1, size,
        )
        vals_hi = book.val[i][take[:top_n], PRIMARY_IX]
        vals_lo = book.val[i][take[-top_n:], PRIMARY_IX]
        vals_hi = vals_hi[~np.isnan(vals_hi)]
        vals_lo = vals_lo[~np.isnan(vals_lo)]
        if not vals_hi.size or not vals_lo.size:
            continue
        daily.append(float(np.mean(vals_hi) - np.mean(vals_lo)))
    return {
        "days": len(daily),
        "spread_pct": round(float(np.mean(daily)) if daily else 0.0, 3),
        "t": round(_t_stat(daily), 2),
    }


def count_buckets(book: Book, idx_list: list[int]) -> list[dict]:
    """按「命中触发条件个数」分层，看主口径超额是否随个数单调。"""
    rows: list[dict] = []
    for target in range(1, len(KEYS) + 1):
        daily: list[float] = []
        pooled: list[float] = []
        for i in idx_list:
            selected = np.nonzero(np.rint(book.counts[i]) == target)[0]
            if not selected.size:
                continue
            values = book.val[i][selected, PRIMARY_IX]
            values = values[~np.isnan(values)]
            if not values.size:
                continue
            pooled.extend(values.tolist())
            bench = book.bench_mean[i][PRIMARY_IX]
            if not np.isnan(bench):
                daily.append(float(np.mean(values)) - float(bench))
        if pooled:
            rows.append(
                {
                    "triggers": str(target),
                    "n": len(pooled),
                    "mean_pct": round(float(np.mean(pooled)), 3),
                    "excess_pct": round(float(np.mean(daily)) if daily else 0.0, 3),
                    "t": round(_t_stat(daily), 2),
                }
            )
    return rows
