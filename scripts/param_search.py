"""雷达参数寻优：调整触发条件 / 权重，跑训练 + 留出样本外验证（研究工具）。

回答的问题
----------
「换个触发器权重或阈值，雷达能不能真的跑赢同池等权基准？」

做法：先在一个**训练段**里搜索权重与阈值，再在完全隔离的**留出段**上只跑一次。
如果训练段最优的方案在留出段上垮掉，说明那只是噪声。为了判断「训练段最优」值不值得
当真，还会跑 30 组随机对照，给出同样本量下纯噪声能达到的 t 值区间。

用法::

    python scripts/param_search.py                        # 240 天窗口，训练 100 / 留出 60
    python scripts/param_search.py --eval-days 120 --train 60 --hold 40
    python scripts/param_search.py --reuse                # 复用缓存，只重跑网格
    python scripts/param_search.py --cache 我的缓存.pkl --out outputs/结果.json

    # D10：留出段默认封存。只有先写预登记、再显式揭盲一次才能看到留出结果
    python scripts/param_search.py --preregistration radar-v2-future-holdout

第一次运行要逐日重放全市场（171 个评估日实测约 6 分钟，缓存约 15MB，落在
``<项目根>/.cache/`` 下，已在 .gitignore 里）。报告同时写到 ``outputs/``。

**留出段纪律（D10）**：不给 ``--preregistration`` 时，训练段照跑，留出段
**既不打印也不落盘**（报告里对应字段为 ``<<BLINDED:...>>``）。给了预登记则
**一次性揭盲**：揭盲时间立刻写回预登记文件，第二次运行会被拒绝。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

try:  # 让脚本在被 import（例如测试）时也能工作：stdout 可能不可重配
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# 必须在 import app.* 之前切到 backend 并放进 sys.path：SQLAlchemy 在
# ``create_engine`` 时就把 sqlite:///./a_stock.db 解析成了相对路径，晚一步就会
# 连到项目根目录下的空库（no such table）。
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.realtime.param_search import (  # noqa: E402
    CONTROL_SALTS,
    DEGENERATE_IN_POOL,
    H_PRIMARY,
    HORIZONS,
    KEYS,
    Book,
    build_cache,
    count_buckets,
    evaluate,
    factor_stats,
    hit_rate,
    top_bottom_spread,
)
from app.realtime.screener import TRIGGER_LABELS, TRIGGER_WEIGHTS  # noqa: E402
from app.research.preregistration import Preregistration  # noqa: E402

EQUAL_WEIGHTS = {k: 100.0 / len(KEYS) for k in KEYS}

#: 预登记产物目录（D10）
PREREG_DIR = ROOT / "docs" / "evidence" / "preregistrations"


def _resolve_preregistration(spec: str) -> Path | None:
    """把 ``--preregistration`` 的取值解析成预登记 JSON 路径。"""
    candidate = Path(spec)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        rooted = ROOT / spec
        if rooted.is_file():
            return rooted
    path = PREREG_DIR / f"{spec}.json"
    if path.is_file():
        return path
    available = sorted(p.stem for p in PREREG_DIR.glob("*.json")) if PREREG_DIR.is_dir() else []
    print(f"[拒绝揭盲] 找不到预登记 {spec!r}；可用：{available or '（目录为空）'}")
    return None


def _line(stats: dict) -> str:
    s = stats[H_PRIMARY]
    return (f"超额 {s.mean_excess_pct:>7.3f}%  t={s.excess_t_stat:>5.2f}  "
            f"命中率 {s.hit_rate:.3f}  n={s.observations:>5}")


def _all_horizons(stats: dict) -> str:
    return "  ".join(
        f"{h}日 {stats[h].mean_excess_pct:>7.3f}%(t={stats[h].excess_t_stat:>5.2f})"
        for h in HORIZONS
    )


def run_search(
    book: Book,
    train_days: int,
    hold_days: int,
    log=print,
    allow_holdout: bool = True,
    planned_trials: int | None = None,
) -> dict:
    """训练 / 留出切分下的完整搜索，返回可直接落盘的报告。

    ``allow_holdout=False``（默认路径）时：留出段的结果**既不打印也不落盘**。
    这不是"打码"，而是让未登记的实验根本无法看到样本外数字 —— 否则留出段
    可以反复查看，样本外结论就失效了（D10）。
    """
    total = len(book)
    if total < train_days + hold_days:
        raise ValueError(
            f"缓存只有 {total} 个评估日，装不下 train={train_days}+hold={hold_days}"
        )

    def _h(msg: str) -> None:
        """留出段输出闸门：未获揭盲授权时整行不输出。"""
        if allow_holdout:
            log(msg)

    gap = total - train_days - hold_days
    train_idx = list(range(0, train_days))
    hold_idx = list(range(total - hold_days, total))
    train_bench = book.pooled_bench_for(train_idx)
    hold_bench = book.pooled_bench_for(hold_idx)
    log(f"缓存 {total} 个评估日（{book.dates[0]} ~ {book.dates[-1]}，复权 {book.adjust}，"
        f"股票池 {book.universe} 只，快照 {book.snapshot_day}）")
    log(f"训练段 {book.dates[train_idx[0]]} ~ {book.dates[train_idx[-1]]}（{len(train_idx)} 天）")
    log(f"隔离带 {gap} 天（避免持有期重叠泄漏，最长持有期 {max(HORIZONS)} 天）")
    log(f"留出段 {book.dates[hold_idx[0]]} ~ {book.dates[hold_idx[-1]]}（{len(hold_idx)} 天）"
        + ("" if allow_holdout else "  ← 已封存，本次不揭盲"))
    _h(f"基准（同池等权）{H_PRIMARY} 日："
       f"训练 {np.mean(np.asarray(train_bench[1])):.3f}%  "
       f"留出 {np.mean(np.asarray(hold_bench[1])):.3f}%\n")

    results: list[dict] = []
    report: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bars_adjust": book.adjust,
        "universe_size": book.universe,
        "symbol_snapshot_day": book.snapshot_day,
        "eval_days": total,
        "train_days": train_days,
        "hold_days": hold_days,
        "purge_days": gap,
        "train_span": [book.dates[train_idx[0]], book.dates[train_idx[-1]]],
        "hold_span": [book.dates[hold_idx[0]], book.dates[hold_idx[-1]]],
        "primary_horizon": H_PRIMARY,
        "caveats": [
            "幸存者偏差：本地只有一份「当前」股票池快照，历史上已退市的标的不在样本内。",
            "窗口重叠：相邻评估日的 k 日收益互相重叠，t 统计量按独立样本读会偏高。",
            "样本区间只有本地日线覆盖的一段，跨风格、跨牛熊代表性不足。",
            "多重检验：网格搜索本身会挑出「训练段最好」的方案，必须看留出段是否复现。",
            "分析结果仅用于研究，不构成投资建议。",
        ],
    }

    def run(name: str, kind: str, weights: dict, min_triggers: int, top_n: int) -> dict:
        row = {
            "name": name,
            "kind": kind,
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "min_triggers": min_triggers,
            "top_n": top_n,
            "train": evaluate(book, train_idx, train_bench, kind, weights, min_triggers, top_n),
            "hold": evaluate(book, hold_idx, hold_bench, kind, weights, min_triggers, top_n),
        }
        results.append(row)
        return row

    def pack(stats: dict) -> dict:
        return {
            str(h): {
                "n": stats[h].observations,
                "net_pct": stats[h].mean_net_pct,
                "bench_pct": stats[h].mean_benchmark_pct,
                "excess_pct": stats[h].mean_excess_pct,
                "t": stats[h].excess_t_stat,
                "hit_rate": stats[h].hit_rate,
                "days": stats[h].excess_days,
                "max_dd_pct": stats[h].max_excess_drawdown_pct,
            }
            for h in HORIZONS
        }

    # ── 1. 单因子体检：每个条件单独当策略用 ──
    log("=== 单因子（各自满分 100，min_triggers=1，top_n=10）===")
    singles: dict[str, dict] = {}
    for key in KEYS:
        weights = {k: (100.0 if k == key else 0.0) for k in KEYS}
        row = run(f"single:{key}", "weights", weights, 1, 10)
        singles[key] = row
        tr, ho = row["train"][H_PRIMARY], row["hold"][H_PRIMARY]
        log(f"  {key:<20} 训练 {tr.mean_excess_pct:>7.3f}%(t={tr.excess_t_stat:>5.2f})")
        _h(f"  {key:<20} 留出 {ho.mean_excess_pct:>7.3f}%(t={ho.excess_t_stat:>5.2f})")

    # ── 2. 现役方案与等权基准 ──
    log("\n=== 基准方案（top_n=10）===")
    for name, weights, min_triggers in (
        ("生产权重(现役) min3", dict(TRIGGER_WEIGHTS), 3),
        ("等权 min3", EQUAL_WEIGHTS, 3),
        ("等权 min2", EQUAL_WEIGHTS, 2),
    ):
        row = run(name, "weights", weights, min_triggers, 10)
        log(f"  {name:<18} 训练 {_line(row['train'])}")
        _h(f"  {'':<18} 留出 {_line(row['hold'])}")

    # ── 3. 只用训练段信息推导权重（留出段不参与任何选择）──
    pos = {k: singles[k]["train"][H_PRIMARY].mean_excess_pct for k in KEYS}
    pos_t = {k: singles[k]["train"][H_PRIMARY].excess_t_stat for k in KEYS}
    log("\n训练段单因子超额（推导权重用）:")
    for key in KEYS:
        log(f"  {key:<20} {pos[key]:>7.3f}%  t={pos_t[key]:>5.2f}")
    keep = [k for k in KEYS if pos[k] > 0]
    others = [k for k in KEYS if k not in DEGENERATE_IN_POOL]
    total_pos = sum(v for v in pos.values() if v > 0) or 1.0
    total_t = sum(pos_t.values()) or 1.0
    total_inv = sum(-v for v in pos.values() if v < 0) or 1.0
    weight_sets = {
        "超额加权": ({k: (pos[k] if pos[k] > 0 else 0.0) / total_pos * 100.0 for k in KEYS}, 2),
        "超额加权 min3": ({k: (pos[k] if pos[k] > 0 else 0.0) / total_pos * 100.0 for k in KEYS}, 3),
        "t值加权": ({k: 100.0 * pos_t[k] / total_t for k in KEYS}, 2),
        "仅正超额条件等权": ({k: (100.0 / len(keep) if k in keep else 0.0) for k in KEYS}, 2),
        "反向超额加权": ({k: (-pos[k] if pos[k] < 0 else 0.0) / total_inv * 100.0 for k in KEYS}, 2),
        "仅可区分条件等权": ({k: (100.0 / len(others) if k in others else 0.0) for k in KEYS}, 2),
    }
    log(f"  训练段为正的条件: {keep}")
    log(f"  池内几乎恒为真、按定义无区分度: {list(DEGENERATE_IN_POOL)}")
    log("\n=== 训练段推导出的权重方案（留出段只跑一次）===")
    for name, (weights, min_triggers) in weight_sets.items():
        row = run(name, "weights", weights, min_triggers, 10)
        tr, ho = row["train"][H_PRIMARY], row["hold"][H_PRIMARY]
        log(f"  {name:<18} 训练 {tr.mean_excess_pct:>7.3f}%(t={tr.excess_t_stat:>5.2f})")
        _h(f"  {name:<18} 留出 {ho.mean_excess_pct:>7.3f}%(t={ho.excess_t_stat:>5.2f})")

    # ── 4. 阈值小网格 ──
    log("\n=== 阈值网格（min_triggers × top_n）===")
    best: dict | None = None
    for weight_name, weights in (("生产", dict(TRIGGER_WEIGHTS)), ("等权", EQUAL_WEIGHTS),
                                 ("超额加权", weight_sets["超额加权"][0])):
        for min_triggers in (1, 2, 3, 4):
            for top_n in (5, 10, 20):
                row = run(f"grid:{weight_name}:min{min_triggers}:top{top_n}", "weights",
                          weights, min_triggers, top_n)
                tr, ho = row["train"][H_PRIMARY], row["hold"][H_PRIMARY]
                log(f"  {weight_name:<8} min={min_triggers} top_n={top_n:>2}"
                    f" 训练 {tr.mean_excess_pct:>7.3f}%(t={tr.excess_t_stat:>5.2f})"
                    f" n={tr.observations:>5}")
                _h(f"  {weight_name:<8} min={min_triggers} top_n={top_n:>2}"
                   f" | 留出 {ho.mean_excess_pct:>7.3f}%(t={ho.excess_t_stat:>5.2f})")
                if best is None or tr.excess_t_stat > best["train"][H_PRIMARY].excess_t_stat:
                    best = row
    assert best is not None

    # ── 5. 对照与噪声带 ──
    log(f"\n=== 对照与噪声带（random {CONTROL_SALTS} 个盐值 / worst）===")
    rand_train, rand_hold = [], []
    for salt in range(1, CONTROL_SALTS + 1):
        book.resalt(salt)
        row = run(f"random:salt{salt}", "random", dict(TRIGGER_WEIGHTS), 3, 10)
        rand_train.append(row["train"][H_PRIMARY].excess_t_stat)
        rand_hold.append(row["hold"][H_PRIMARY].excess_t_stat)
    book.resalt(0)
    run("random:salt0", "random", dict(TRIGGER_WEIGHTS), 3, 10)
    worst = run("worst", "worst", dict(TRIGGER_WEIGHTS), 3, 10)
    log(f"  random 训练 t：均值 {np.mean(rand_train):>5.2f} 标准差 {np.std(rand_train):.2f} "
        f"区间 [{min(rand_train):.2f}, {max(rand_train):.2f}]")
    _h(f"  random 留出 t：均值 {np.mean(rand_hold):>5.2f} 标准差 {np.std(rand_hold):.2f} "
       f"区间 [{min(rand_hold):.2f}, {max(rand_hold):.2f}]")
    log(f"  worst  训练 {_line(worst['train'])}")
    _h(f"  worst  留出 {_line(worst['hold'])}")

    # ── 6. 训练段最优 → 留出段一次性确认 ──
    _h("\n=== 训练段最优方案 → 留出确认 ===")
    log(f"  训练段最优 {best['name']} 训练 {_line(best['train'])}")
    _h(f"  留出结果 {_line(best['hold'])}")
    _h(f"  留出各持有期 {_all_horizons(best['hold'])}")

    # ── 7. 因子诊断：条件本身有没有区分度 ──
    log("\n=== 因子诊断（候选池内命中 vs 未命中，主口径）===")
    log(f"{'触发器':<20}{'池内命中率(训练)':>16}{'训练n':>8}{'训练差':>9}{'训练t':>8}")
    diagnostics = []
    for key in KEYS:
        tr = factor_stats(book, train_idx, key)
        ho = factor_stats(book, hold_idx, key)
        rate_tr = hit_rate(book, train_idx, key)
        rate_ho = hit_rate(book, hold_idx, key)
        diagnostics.append({
            "key": key, "label": TRIGGER_LABELS.get(key, key),
            "hit_rate_train": round(rate_tr, 4), "hit_rate_hold": round(rate_ho, 4),
            "train": tr, "hold": ho,
        })
        log(f"{key:<20}{rate_tr:>16.2f}{tr['n_hit']:>8}{tr['spread_pct']:>9.3f}"
            f"{tr['t']:>8.2f}")
        _h(f"{key:<20}[留出] 命中率 {rate_ho:.2f} n={ho['n_hit']} "
           f"差 {ho['spread_pct']:.3f}% t={ho['t']:.2f}")

    log("\n=== 同池多空价差（打分最高 10 只 − 最低 10 只）===")
    spreads = {}
    for name, weights in (("生产权重", dict(TRIGGER_WEIGHTS)), ("等权", EQUAL_WEIGHTS)):
        tr = top_bottom_spread(book, train_idx, weights)
        ho = top_bottom_spread(book, hold_idx, weights)
        spreads[name] = {"train": tr, "hold": ho}
        log(f"  {name:<8} 训练 价差 {tr['spread_pct']:>7.3f}%(t={tr['t']:>5.2f})")
        _h(f"  {name:<8} 留出 价差 {ho['spread_pct']:>7.3f}%(t={ho['t']:>5.2f})")

    log("\n=== 按「命中条件个数」分层（主口径超额，相对当日基准）===")
    buckets_train = count_buckets(book, train_idx)
    buckets_hold = count_buckets(book, hold_idx)
    log(f"{'个数':>4}{'训练n':>8}{'训练超额':>10}{'训练t':>8}")
    for tr, ho in zip(buckets_train, buckets_hold):
        log(f"{tr['triggers']:>4}{tr['n']:>8}{tr['excess_pct']:>10.3f}{tr['t']:>8.2f}")
        _h(f"{ho['triggers']:>4}[留出] n={ho['n']} 超额 {ho['excess_pct']:.3f}% t={ho['t']:.2f}")

    report.update({
        "results": [
            {k: (pack(v) if k in ("train", "hold") else v) for k, v in row.items()}
            for row in results
        ],
        "train_single_factor": {k: pack(singles[k]["train"]) for k in KEYS},
        "hold_single_factor": {k: pack(singles[k]["hold"]) for k in KEYS},
        "train_positive_conditions": keep,
        "degenerate_in_pool": list(DEGENERATE_IN_POOL),
        "noise_band": {
            "salt_count": CONTROL_SALTS,
            "train_t_mean": round(float(np.mean(rand_train)), 2),
            "train_t_sd": round(float(np.std(rand_train)), 2),
            "train_t_min": round(min(rand_train), 2),
            "train_t_max": round(max(rand_train), 2),
            "hold_t_mean": round(float(np.mean(rand_hold)), 2),
            "hold_t_sd": round(float(np.std(rand_hold)), 2),
            "hold_t_min": round(min(rand_hold), 2),
            "hold_t_max": round(max(rand_hold), 2),
            "hold_t_values": [round(v, 2) for v in rand_hold],
        },
        "best_on_train": {
            "name": best["name"], "kind": best["kind"], "weights": best["weights"],
            "min_triggers": best["min_triggers"], "top_n": best["top_n"],
            "train": pack(best["train"]), "hold": pack(best["hold"]),
            "hold_all_horizons": _all_horizons(best["hold"]),
        },
        "factor_diagnostics": diagnostics,
        "top_bottom_spread": spreads,
        "count_buckets_train": buckets_train,
        "count_buckets_hold": buckets_hold,
    })
    report["multi_comparison"] = _multi_comparison_note(results, planned_trials)
    if not allow_holdout:
        redact_holdout(report)
    return report


#: 未揭盲时留出段内容被替换成的占位符
BLINDED = "<<BLINDED:holdout-not-unblinded>>"


def redact_holdout(report: dict) -> dict:
    """未获揭盲授权时，把报告里的留出段内容整段抹掉。

    只抹结果、保留窗口元信息（`hold_span`）—— 知道留出段是哪几天不算偷看，
    看到留出段的数字才算。
    """
    for row in report.get("results", []):
        if isinstance(row, dict) and "hold" in row:
            row["hold"] = BLINDED
    hold_single = report.get("hold_single_factor")
    if isinstance(hold_single, dict):
        report["hold_single_factor"] = {k: BLINDED for k in hold_single}
    report["count_buckets_hold"] = BLINDED
    for item in report.get("factor_diagnostics") or []:
        if isinstance(item, dict):
            item["hold"] = BLINDED
            item["hit_rate_hold"] = BLINDED
    spreads = report.get("top_bottom_spread")
    if isinstance(spreads, dict):
        for item in spreads.values():
            if isinstance(item, dict) and "hold" in item:
                item["hold"] = BLINDED
    best = report.get("best_on_train")
    if isinstance(best, dict):
        best["hold"] = BLINDED
        best["hold_all_horizons"] = BLINDED
    noise = report.get("noise_band")
    if isinstance(noise, dict):
        for key in [k for k in noise if k.startswith("hold")]:
            noise[key] = BLINDED
    report["holdout_blinded"] = True
    report["holdout_blind_reason"] = (
        "未提供 --preregistration（或该预登记已揭盲过）：留出段内容已整段封存。"
        "按 D10 纪律，留出段只允许在预登记后揭盲一次。"
    )
    return report


def _multi_comparison_note(results: list[dict], planned_trials: int | None = None) -> dict:
    """把「试了多少次」换算成校正后的显著性门槛（正态近似，仅作提示）。

    网格搜索本身会挑出训练段最好的方案：不做校正就会把「试出来的最好」
    当成「显著」。这里用训练段 t 值正态近似算出名义 p 值，再按
    max(实际行数, 冻结的 planned_trials) 做 Benjamini-Hochberg 校正。
    """
    from math import erfc, sqrt

    from app.research.preregistration import adjust_pvalues, summarize_correction

    pvalues = [
        erfc(abs(row["train"][H_PRIMARY].excess_t_stat) / sqrt(2.0))
        for row in results
        if row.get("train") and H_PRIMARY in row["train"]
    ]
    m = max(len(pvalues), int(planned_trials or 0))
    if m == 0:
        return {"method": "benjamini_hochberg", "m": 0, "note": "无可用 t 值"}
    # 预登记声明的试验次数可能多于本脚本一次跑出的行数；未观测到的试验按 p=1
    # 保守填充，保证校正用的 m 与「实际试了多少次」一致（否则会把门槛放得太松）。
    padded = list(pvalues) + [1.0] * (m - len(pvalues))
    correction = adjust_pvalues(padded, "benjamini_hochberg", alpha=0.05)
    correction["m"] = m
    correction["observed_trials"] = len(pvalues)
    correction["nominal_significant"] = sum(1 for p in pvalues if p <= 0.05)
    correction["note"] = (
        summarize_correction(correction)
        + f"（校正按 m={m} 次试验；本次实际产出 {len(pvalues)} 行，"
        "其余按 p=1 保守填充。p 值由训练段 t 值正态近似得到，不是精确 t 分布；"
        "本块只用于提醒「试了 m 次」，不能替代留出段验证）"
    )
    return correction


def main() -> int:
    parser = argparse.ArgumentParser(description="雷达参数寻优（训练 + 留出样本外验证）")
    parser.add_argument("--eval-days", type=int, default=240, help="可评估窗口上限，默认 240")
    parser.add_argument("--train", type=int, default=100, help="训练段评估日数，默认 100")
    parser.add_argument("--hold", type=int, default=60, help="留出段评估日数，默认 60")
    parser.add_argument("--end-day", default="",
                        help="评估窗口右端（YYYY-MM-DD，含）；缺省取最近 --eval-days 个交易日。"
                             "用于跨年份 walk-forward：让窗口整体滑动到历史某个时点")
    parser.add_argument("--cache", default="", help="缓存文件路径，默认 <项目根>/.cache/param_search_<窗口>.pkl")
    parser.add_argument("--reuse", action="store_true", help="复用已有缓存，不重新重放")
    parser.add_argument("--out", default="", help="报告输出路径，默认 outputs/param_search_<时间戳>.json")
    parser.add_argument("--preregistration", default="",
                        help="预登记：experiment_id 或 JSON 路径。提供后**一次性**揭盲留出段；"
                             "不提供则留出段整段封存，只跑训练段")
    args = parser.parse_args()

    # ── D10 预登记门禁：默认不揭盲 ──
    allow_holdout = False
    prereg_block: dict = {
        "provided": False,
        "note": "未提供 --preregistration：留出段整段封存（D10 纪律）",
    }
    if args.preregistration:
        path = _resolve_preregistration(args.preregistration)
        if path is None:
            return 2
        try:
            record = Preregistration.load(path)
        except (ValueError, OSError) as exc:
            print(f"[拒绝揭盲] 预登记无法加载：{exc}")
            return 2
        decision = record.unblind(note=f"param_search.py 揭盲（train={args.train} hold={args.hold}）")
        if not decision.get("allowed"):
            print(f"[拒绝揭盲] {decision.get('reason')}")
            print("本次只运行训练段；留出段保持封存。")
            prereg_block = {
                "provided": True,
                "experiment_id": record.experiment_id,
                "frozen_hash": record.frozen_hash,
                "unblind_allowed": False,
                "first_unblinded_at": decision.get("first_unblinded_at"),
                "note": decision.get("reason"),
            }
        else:
            # 先落盘再跑：中途崩溃也不会让同一份预登记被看第二次
            record.save(path)
            allow_holdout = True
            print(f"[已揭盲] {record.experiment_id}")
            print(f"  冻结哈希 {record.frozen_hash}")
            print(f"  计划试验 {record.payload.get('planned_trials')} 次，"
                  f"多重比较 {record.payload.get('multiple_comparison')}")
            print(f"  揭盲时间 {decision.get('unblinded_at')}（已写入 {path}，不可重复揭盲）")
            prereg_block = {
                "provided": True,
                "experiment_id": record.experiment_id,
                "frozen_hash": record.frozen_hash,
                "unblind_allowed": True,
                "unblinded_at": decision.get("unblinded_at"),
                "planned_trials": record.payload.get("planned_trials"),
                "multiple_comparison": record.payload.get("multiple_comparison"),
                "primary_horizon_days": record.payload.get("primary_horizon_days"),
                "primary_benchmark": record.payload.get("primary_benchmark"),
                "path": str(path),
            }
    else:
        print("[留出段封存] 未提供 --preregistration：本次不揭盲，只输出训练段。")
        print("  按 D10 纪律：先写预登记（docs/evidence/preregistrations/），再揭盲一次。")

    end_day = date.fromisoformat(args.end_day) if args.end_day else None
    suffix = f"_{end_day.isoformat()}" if end_day is not None else ""
    cache_path = Path(args.cache) if args.cache else (
        ROOT / ".cache" / f"param_search_{args.eval_days}{suffix}.pkl"
    )
    started = time.perf_counter()
    if args.reuse and cache_path.exists():
        print(f"复用缓存 {cache_path}")
        with cache_path.open("rb") as fh:
            payload = pickle.load(fh)
    else:
        payload = build_cache(args.eval_days, end_day=end_day)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as fh:
            pickle.dump(payload, fh, protocol=5)
        print(f"缓存已写入 {cache_path}（{cache_path.stat().st_size / 1e6:.1f}MB，"
              f"累计 {time.perf_counter() - started:.0f}s）")

    book = Book(payload)
    report = run_search(
        book,
        args.train,
        args.hold,
        allow_holdout=allow_holdout,
        planned_trials=prereg_block.get("planned_trials"),
    )
    report["cache_file"] = str(cache_path)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    report["preregistration"] = prereg_block
    report["holdout_visible"] = allow_holdout

    out = Path(args.out) if args.out else (
        ROOT / "outputs" / f"param_search_{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"\n报告已写入 {out}")
    print("分析结果仅用于研究，不构成投资建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
