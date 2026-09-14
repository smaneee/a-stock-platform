"""P0-01 情绪因子验证器 —— 修正后的可复用核心。

本模块只做纯计算与守卫，不碰数据库（数据库读取在 :mod:`sent_factor_v2`）。
设计目标是把《研发计划-2026Q4》1.2 / 11.2 节列出的五个口径缺陷变成**可测的代码约束**：

============================  ==================================================
旧口径缺陷                     本模块对应的守卫
============================  ==================================================
行情按返回顺序推进未来收益     :func:`ensure_sorted_unique_dates` 强制显式升序
                              + 唯一性；重复日期抛 :class:`DuplicateTradeDateError`
Spearman 两次 argsort、无并列秩 :func:`spearman_avg_rank`（平均秩）+
                              :func:`cross_check_spearman` 与 ``scipy.stats.spearmanr``
                              交叉核对（容差 1e-9）
当日收盘情绪 + 同日收盘起算收益  :func:`assert_no_lookahead`：收益起算日必须**严格晚于**
                              信号日，否则抛 :class:`LookAheadError`
derived / eastmoney 混池        :func:`split_by_source` + :class:`SourceMixError`
重叠收益当独立样本              :func:`hac_mean_ci`（Newey-West，登记滞后阶数）与
                              :func:`block_bootstrap_mean_ci`（登记块长）+
                              :func:`sensitivity_table` 做滞后/块长敏感性
============================  ==================================================

术语：
- **信号日 t**：情绪因子取值所属交易日。该因子只有在 **t 日收盘后** 才可得。
- **建仓日 t+1**：最早的、现实可执行的建仓时点（t+1 开盘）。
- **收益起算日**：组合收益的起算时点。本模块一律要求 > t。
- **标签结束时点**：``t+k`` 收盘。样本划分的隔离带必须覆盖它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, sqrt
from typing import Iterable, Mapping, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SentFactorError(Exception):
    """本模块所有可预期错误的基类。"""


class DuplicateTradeDateError(SentFactorError):
    """同一 (来源, trade_date) 出现多行。旧脚本静默取一条，这里必须报错。"""


class LookAheadError(SentFactorError):
    """信号日晚于或等于收益起算日 —— 未来函数。"""


class SourceMixError(SentFactorError):
    """把 derived 与 eastmoney 混进同一池统计。"""


class InsufficientSampleError(SentFactorError):
    """样本量不足以支撑带方向的结论。"""


# ---------------------------------------------------------------------------
# 1. 显式排序 + 唯一性
# ---------------------------------------------------------------------------


def ensure_sorted_unique_dates(
    rows: Sequence[Mapping[str, object]],
    *,
    date_key: str = "trade_date",
    source_key: str | None = "source",
    require_unique: bool = True,
) -> list[Mapping[str, object]]:
    """按 ``date_key`` 升序排列，并对重复日期做显式断言。

    参数
    ----
    rows
        已经按某种顺序取得的行（例如 ``sqlite3`` 返回顺序，**不可信**）。
    require_unique
        ``True``（默认）时，同一 ``(source, date)`` 出现多行即抛
        :class:`DuplicateTradeDateError`；``False`` 时只排序、不检查。

    返回
    ----
    新的、按日期升序的列表（不修改入参）。

    说明
    ----
    排序键显式用 ``str(date)`` 归一化，避免 ``datetime.date`` 与
    ``str`` 混排时的隐式比较错误。
    """
    ordered = sorted(rows, key=lambda row: _norm_date(row[date_key]))
    if not require_unique:
        return ordered

    seen: dict[tuple[object, object], int] = {}
    for row in ordered:
        key = (
            row[source_key] if source_key is not None else None,
            _norm_date(row[date_key]),
        )
        seen[key] = seen.get(key, 0) + 1
    duplicates = [
        (src, day, count) for (src, day), count in seen.items() if count > 1
    ]
    if duplicates:
        duplicates.sort(key=lambda item: (str(item[0]), str(item[1])))
        detail = ", ".join(
            f"{src or '-'}@{day} x{count}" for src, day, count in duplicates[:10]
        )
        raise DuplicateTradeDateError(
            f"同一 (来源, 交易日) 出现多行，拒绝静默取一条：{detail}"
            + (" …" if len(duplicates) > 10 else "")
        )
    return ordered


def _norm_date(value: object) -> str:
    """把 date / datetime / str 统一成 ``YYYY-MM-DD`` 字符串，供排序与比较。"""
    if value is None:
        raise SentFactorError("日期字段为 None")
    if isinstance(value, str):
        return value[:10]
    return value.isoformat()[:10]  # type: ignore[union-attr]


def monotonic_dates(rows: Sequence[Mapping[str, object]], *, date_key: str = "trade_date") -> bool:
    """判断给定行序列是否已按日期严格递增。"""
    keys = [_norm_date(row[date_key]) for row in rows]
    return all(a < b for a, b in zip(keys, keys[1:]))


def split_by_source(
    rows: Sequence[Mapping[str, object]], *, source_key: str = "source"
) -> dict[str, list[Mapping[str, object]]]:
    """按来源切分并各自排序；**禁止**把不同来源合池统计。

    返回 ``{source: rows}``，每个列表内部已按日期升序且唯一。
    """
    buckets: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        src = str(row.get(source_key) or "unknown")
        buckets.setdefault(src, []).append(row)
    return {
        src: ensure_sorted_unique_dates(items, source_key=source_key)
        for src, items in sorted(buckets.items())
    }


def assert_single_source(rows: Sequence[Mapping[str, object]], *, source_key: str = "source") -> str:
    """断言一组行只含一个来源，返回该来源名。"""
    sources = sorted({str(row.get(source_key) or "unknown") for row in rows})
    if len(sources) != 1:
        raise SourceMixError(f"检测到多来源混池：{sources}；必须分源统计")
    return sources[0]


# ---------------------------------------------------------------------------
# 2. 并列秩 + Spearman（自实现）与 scipy 交叉核对
# ---------------------------------------------------------------------------


def average_rank(values: Sequence[float]) -> np.ndarray:
    """返回平均秩（1 基）。并列值取平均秩。

    与 ``scipy.stats.rankdata(method='average')`` 等价，本函数刻意自实现，
    以便在不依赖 scipy 时也能工作，并用作交叉核对的独立实现。
    """
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise SentFactorError("average_rank 只接受一维序列")
    n = arr.size
    if n == 0:
        return np.empty(0, dtype=float)
    order = np.argsort(arr, kind="mergesort")  # 稳定排序：并列值顺序无关
    ranks = np.empty(n, dtype=float)
    sorted_vals = arr[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        # 位置 i..j（0 基）共享秩 (i+1 + j+1)/2
        ranks[order[i : j + 1]] = 0.5 * ((i + 1) + (j + 1))
        i = j + 1
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """皮尔逊相关系数；任一序列为常数时返回 ``nan``。"""
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    if a.size != b.size:
        raise SentFactorError(f"长度不一致：{a.size} vs {b.size}")
    if a.size < 2:
        return float("nan")
    da = a - a.mean()
    db = b - b.mean()
    denom = sqrt(float(da @ da) * float(db @ db))
    if denom == 0.0:
        return float("nan")
    return float(da @ db) / denom


def spearman_avg_rank(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman 秩相关（平均秩、并列值 corrected）。

    实现 = 平均秩上的皮尔逊相关。旧脚本「两次 argsort」相当于给并列值分配了
    任意且不一致的秩，会系统性高估/低估相关性。
    """
    return pearson(average_rank(x), average_rank(y))


def cross_check_spearman(
    x: Sequence[float], y: Sequence[float], *, tol: float = 1e-9
) -> dict[str, object]:
    """用自实现与 ``scipy.stats.spearmanr`` 交叉核对。

    返回字典含 ``self_value`` / ``scipy_value`` / ``abs_diff`` / ``ok`` / ``scipy_available``。
    scipy 未安装时 ``scipy_available=False``、``ok=None``，并在 ``note`` 中说明。
    """
    self_value = spearman_avg_rank(x, y)
    out: dict[str, object] = {
        "self_value": self_value,
        "n": int(np.asarray(x).size),
        "tol": tol,
    }
    try:
        from scipy import stats  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 仅在没有 scipy 的环境触发
        out.update(
            scipy_available=False,
            scipy_value=None,
            abs_diff=None,
            ok=None,
            note=f"scipy 不可用（{exc.__class__.__name__}），仅自实现结果",
        )
        return out

    res = stats.spearmanr(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    scipy_value = float(res.statistic)
    diff = (
        abs(self_value - scipy_value)
        if np.isfinite(self_value) and np.isfinite(scipy_value)
        else float("inf")
    )
    out.update(
        scipy_available=True,
        scipy_value=scipy_value,
        scipy_pvalue=float(res.pvalue),
        abs_diff=diff,
        ok=bool(diff <= tol),
        note="自实现平均秩 Spearman 与 scipy 一致" if diff <= tol else "不一致，必须排查",
    )
    return out


# ---------------------------------------------------------------------------
# 3. 可用时点（禁止未来函数）
# ---------------------------------------------------------------------------


def assert_no_lookahead(
    signal_dates: Iterable[object],
    entry_dates: Iterable[object],
    *,
    exit_dates: Iterable[object] | None = None,
) -> None:
    """断言每个样本的建仓日**严格晚于**信号日。

    三个序列等长、一一对应。任何 ``entry <= signal`` 都抛 :class:`LookAheadError`，
    因为那意味着用了当日收盘才生成的信号去交易当日（含当日开盘）。
    """
    sig = [_norm_date(d) for d in signal_dates]
    ent = [_norm_date(d) for d in entry_dates]
    if len(sig) != len(ent):
        raise SentFactorError("signal_dates 与 entry_dates 长度不一致")
    bad = [
        (s, e)
        for s, e in zip(sig, ent)
        if e <= s
    ]
    if bad:
        sample = ", ".join(f"{s}->{e}" for s, e in bad[:5])
        raise LookAheadError(
            f"检测到未来函数：建仓日不晚于信号日（{len(bad)} 例，如 {sample}）"
        )
    if exit_dates is not None:
        ex = [_norm_date(d) for d in exit_dates]
        if len(ex) != len(sig):
            raise SentFactorError("exit_dates 长度不一致")
        bad_exit = [(e, x) for e, x in zip(ent, ex) if x < e]
        if bad_exit:
            raise LookAheadError(f"收益结束时点早于建仓时点：{bad_exit[:5]}")


def lookahead_violations(
    signal_dates: Sequence[object], entry_dates: Sequence[object]
) -> list[tuple[str, str]]:
    """:func:`assert_no_lookahead` 的非抛出版本，返回违规对（供告警/报告用）。"""
    return [
        (_norm_date(s), _norm_date(e))
        for s, e in zip(signal_dates, entry_dates)
        if _norm_date(e) <= _norm_date(s)
    ]


# ---------------------------------------------------------------------------
# 4. 重叠收益：HAC（Newey-West）与块自助法
# ---------------------------------------------------------------------------


def bartlett_weights(lags: int) -> list[float]:
    """Bartlett 核权重 ``1 - j/(L+1)``，``j = 1..L``。"""
    if lags < 0:
        raise SentFactorError("lags 必须 >= 0")
    return [1.0 - j / (lags + 1.0) for j in range(1, lags + 1)]


def hac_mean_ci(
    values: Sequence[float],
    *,
    lags: int,
    z: float = 1.959963984540054,
) -> dict[str, object]:
    """序列均值的 Newey-West (HAC) 置信区间。

    方差用 Bartlett 核：``gamma0 + 2 * sum_j (1 - j/(L+1)) * gamma_j``，
    再除以 ``T``。``lags=0`` 退化为普通 i.i.d. 标准误。

    返回含 ``n`` / ``mean`` / ``lags`` / ``se`` / ``ci_low`` / ``ci_high`` /
    ``t_stat`` / ``kernel``；``n < 3`` 时抛 :class:`InsufficientSampleError`。
    """
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    t = x.size
    if t < 3:
        raise InsufficientSampleError(f"HAC 至少需要 3 个观测，实际 {t}")
    effective_lags = min(lags, t - 2)
    mean = float(x.mean())
    dev = x - mean
    gamma0 = float(dev @ dev) / t
    var = gamma0
    for j, weight in enumerate(bartlett_weights(effective_lags), start=1):
        gamma_j = float(dev[j:] @ dev[:-j]) / t
        var += 2.0 * weight * gamma_j
    var = max(var, 0.0)
    se = sqrt(var / t)
    return {
        "n": int(t),
        "mean": mean,
        "lags": int(effective_lags),
        "lags_requested": int(lags),
        "kernel": "bartlett",
        "se": se,
        "ci_low": mean - z * se,
        "ci_high": mean + z * se,
        "t_stat": (mean / se) if se > 0 else float("nan"),
    }


def block_bootstrap_mean_ci(
    values: Sequence[float],
    *,
    block: int,
    n_boot: int = 2000,
    seed: int = 20260913,
    alpha: float = 0.05,
) -> dict[str, object]:
    """移动块自助法（moving block bootstrap）的均值百分位区间。

    块长 ``block`` 必须登记并在报告中做敏感性分析；``block=1`` 退化为 i.i.d.
    自助法，会低估重叠窗口造成的依赖。重叠 k 日收益至少应取 ``block >= k``。
    """
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    t = x.size
    if t < 3:
        raise InsufficientSampleError(f"块自助法至少需要 3 个观测，实际 {t}")
    if block < 1:
        raise SentFactorError("block 必须 >= 1")
    b = min(block, t)
    n_blocks = ceil(t / b)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, t - b + 1, size=(n_boot, n_blocks))
    idx = starts[:, :, None] + np.arange(b)[None, None, :]
    samples = x[idx.reshape(n_boot, -1)[:, :t]]
    means = samples.mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "n": int(t),
        "mean": float(x.mean()),
        "block": int(b),
        "block_requested": int(block),
        "n_boot": int(n_boot),
        "seed": int(seed),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "boot_se": float(means.std(ddof=1)),
    }


def sensitivity_table(
    values: Sequence[float],
    *,
    lags: Sequence[int] = (0, 1, 5, 10),
    blocks: Sequence[int] = (1, 5, 10, 20),
    n_boot: int = 2000,
    seed: int = 20260913,
) -> dict[str, object]:
    """对 HAC 滞后阶数与块长做敏感性分析（各至少 3 个取值）。"""
    hac = [hac_mean_ci(values, lags=lag) for lag in lags]
    boot = [
        block_bootstrap_mean_ci(values, block=blk, n_boot=n_boot, seed=seed)
        for blk in blocks
    ]
    signs = {np.sign(item["ci_low"]) for item in hac + boot}
    return {
        "n": int(np.asarray([v for v in values if np.isfinite(v)], dtype=float).size),
        "mean": float(np.mean([v for v in values if np.isfinite(v)])),
        "hac": hac,
        "bootstrap": boot,
        "stable_sign": len(signs) == 1,
        "significant_at_95": all(item["ci_low"] > 0 or item["ci_high"] < 0 for item in hac + boot),
    }


def block_bootstrap_spearman_ci(
    x: Sequence[float],
    y: Sequence[float],
    *,
    block: int,
    n_boot: int = 2000,
    seed: int = 20260913,
    alpha: float = 0.05,
) -> dict[str, object]:
    """对秩相关做移动块自助法区间。

    为什么必须有这个：
    ``scipy.stats.spearmanr`` 的 p 值假设样本独立。持有 k 日的收益窗口高度重叠，
    名义 n 天里真正的独立观测大约只有 n/k 个，直接用 scipy 的 p 值会系统性
    高估显著性。这里按块重采样 (x, y) 配对，保留序列依赖。
    """
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    t = a.size
    if t < 3:
        raise InsufficientSampleError(f"秩相关自助法至少需要 3 个观测，实际 {t}")
    if block < 1:
        raise SentFactorError("block 必须 >= 1")
    size = min(block, t)
    # 块长 >= 样本长度时只能取到「整段」这一种重采样，自助分布退化为一个点，
    # 区间宽度为 0，不能用来判断显著性。
    degenerate = bool(size >= t)
    n_blocks = ceil(t / size)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, t - size + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(size)[None, None, :]).reshape(n_boot, -1)[:, :t]
    point = spearman_avg_rank(a, b)
    stats = np.array([spearman_avg_rank(a[row], b[row]) for row in idx], dtype=float)
    stats = stats[np.isfinite(stats)]
    if stats.size == 0:
        raise InsufficientSampleError("自助法全部退化（常数序列）")
    lo, hi = np.percentile(stats, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "n": int(t),
        "point": point,
        "block": int(size),
        "block_requested": int(block),
        "n_boot": int(n_boot),
        "seed": int(seed),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "boot_se": float(stats.std(ddof=1)),
        "degenerate": degenerate,
        "excludes_zero": bool(not degenerate and (lo > 0.0 or hi < 0.0)),
    }


def spearman_sensitivity(
    x: Sequence[float],
    y: Sequence[float],
    *,
    blocks: Sequence[int] = (1, 3, 5, 10, 20),
    n_boot: int = 2000,
    seed: int = 20260913,
) -> dict[str, object]:
    """秩相关在多个块长下的稳健性（块长必须登记）。

    ``robust_excludes_zero`` 只有在**所有**块长下区间都不含 0 时才为 True；
    只要换块长就跨越零，就不能声称相关显著。
    """
    rows = [
        block_bootstrap_spearman_ci(x, y, block=blk, n_boot=n_boot, seed=seed)
        for blk in blocks
    ]
    usable = [r for r in rows if not r["degenerate"]]
    return {
        "point": rows[0]["point"] if rows else None,
        "n": rows[0]["n"] if rows else 0,
        "blocks": list(blocks),
        "intervals": rows,
        "usable_intervals": len(usable),
        # 只要有一个非退化块长的区间跨越零，就不能声称相关显著
        "robust_excludes_zero": bool(usable) and all(r["excludes_zero"] for r in usable),
        "any_includes_zero": any(not r["excludes_zero"] for r in usable),
    }


# ---------------------------------------------------------------------------
# 5. 样本划分（训练 / 选择 / 隔离带 / 最终留出）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """一个样本段。``first_label_end`` 是该段最后一个样本的标签结束时点。"""

    name: str
    dates: tuple[str, ...]

    @property
    def start(self) -> str | None:
        return self.dates[0] if self.dates else None

    @property
    def end(self) -> str | None:
        return self.dates[-1] if self.dates else None

    @property
    def n(self) -> int:
        return len(self.dates)


@dataclass(frozen=True)
class SplitPlan:
    """完整的样本划分方案，含隔离带。"""

    horizon: int
    train: Split
    select: Split
    embargo: Split
    holdout: Split
    gap_before_select: int

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon_k": self.horizon,
            "gap_before_select": self.gap_before_select,
            "embargo_covering_label_end": self.embargo.n,
            "train": {"n": self.train.n, "start": self.train.start, "end": self.train.end},
            "select": {"n": self.select.n, "start": self.select.start, "end": self.select.end},
            "embargo": {"n": self.embargo.n, "dates": list(self.embargo.dates)},
            "holdout": {"n": self.holdout.n, "start": self.holdout.start, "end": self.holdout.end},
        }


def make_splits(
    feature_dates: Sequence[object],
    *,
    horizon: int,
    train_frac: float = 0.5,
    select_frac: float = 0.25,
    min_holdout: int = 30,
) -> SplitPlan:
    """按时间顺序切分「信号日」，隔离带宽度 = ``horizon`` 个交易日。

    隔离带的作用：训练/选择段最后一个样本的标签在 ``t+horizon`` 收盘才结束，
    所以下一段必须从 ``t+horizon`` 之后开始，否则标签区间跨段泄漏。

    样本不足（留出段 < ``min_holdout``）时抛 :class:`InsufficientSampleError`。
    """
    if horizon < 1:
        raise SentFactorError("horizon 必须 >= 1")
    dates = tuple(_norm_date(d) for d in feature_dates)
    if not monotonic_dates([{"trade_date": d} for d in dates]):
        raise SentFactorError("feature_dates 必须先升序且唯一")
    n = len(dates)
    if n == 0:
        raise InsufficientSampleError("没有任何信号日")

    i_train_end = max(1, int(n * train_frac))
    # 隔离带：训练段最后一个标签结束时点之后
    i_select_start = i_train_end + horizon
    i_select_end = max(i_select_start, int(n * (train_frac + select_frac)))
    i_embargo_start = i_select_end
    i_embargo_end = i_embargo_start + horizon
    i_holdout_start = i_embargo_end

    if i_holdout_start >= n:
        raise InsufficientSampleError(
            f"样本不足：n={n}, horizon={horizon} 时没有剩余留出段"
        )
    train = Split("train", dates[:i_train_end])
    select = Split("select", dates[i_select_start:i_select_end])
    embargo = Split("embargo", dates[i_embargo_start:i_embargo_end])
    holdout = Split("holdout", dates[i_holdout_start:])
    if holdout.n < min_holdout:
        raise InsufficientSampleError(
            f"样本不足：最终留出段仅 {holdout.n} 个交易日（要求 >= {min_holdout}），"
            "结论必须标为不确定"
        )
    return SplitPlan(
        horizon=horizon,
        train=train,
        select=select,
        embargo=embargo,
        holdout=holdout,
        gap_before_select=horizon,
    )


def split_integrity_report(plan: SplitPlan, label_end_by_date: Mapping[str, str]) -> dict[str, object]:
    """校验分段之间（含标签区间）没有任何重叠，并给出证据。"""

    def last_label_end(split: Split) -> str | None:
        ends = [label_end_by_date.get(d) for d in split.dates]
        ends = [e for e in ends if e]
        return max(ends) if ends else None

    train_end_label = last_label_end(plan.train)
    select_end_label = last_label_end(plan.select)
    holdout_start = plan.holdout.start
    select_start = plan.select.start
    checks = {
        "train_label_ends_before_select_starts": bool(
            train_end_label and select_start and train_end_label < select_start
        ),
        "select_label_ends_before_holdout_starts": bool(
            select_end_label and holdout_start and select_end_label < holdout_start
        ),
        "embargo_covers_select_label_end": bool(
            select_end_label
            and plan.embargo.end
            and select_end_label <= plan.embargo.end
        ),
        "segments_disjoint": (
            set(plan.train.dates).isdisjoint(plan.select.dates)
            and set(plan.select.dates).isdisjoint(plan.embargo.dates)
            and set(plan.embargo.dates).isdisjoint(plan.holdout.dates)
        ),
    }
    return {
        **checks,
        "all_ok": all(checks.values()),
        "train_last_label_end": train_end_label,
        "select_last_label_end": select_end_label,
        "holdout_start": holdout_start,
        "embargo_dates": list(plan.embargo.dates),
    }


# ---------------------------------------------------------------------------
# 6. 来源标签 / 观察可用时间
# ---------------------------------------------------------------------------


@dataclass
class SourceBlock:
    """一段「同来源」研究样本的元数据，报告里必须逐段注明。"""

    source: str
    n_days: int
    symbols_median: float
    first_date: str
    last_date: str
    algo_version: str
    observed_at: str
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "coverage_days": self.n_days,
            "coverage_symbols_median": self.symbols_median,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "algo_version": self.algo_version,
            "observed_available_at": self.observed_at,
            "limitations": list(self.limitations),
        }
