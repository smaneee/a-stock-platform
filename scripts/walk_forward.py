"""跨年份 walk-forward 样本外验证（研究工具）。

为什么需要它
------------
``param_search.py`` 只在**一个**训练/留出切分上做验证。单一切分的结论很容易
被特定市场风格主导，而且用户要的是「这套规则到底能不能跑赢」这种可复核的答案。
这里把缓存铺满本地全部历史（默认最多 1400 个交易日），再按滚动方式切成多折：

    每折 = [训练段 250 天] + [隔离带 10 天] + [留出段 60 天]
    → 训练段挑「最好」的方案，留出段只跑一次，绝不回头改参数
    → 折间不重叠地向前滚动（--step 控制步长）

汇总三类结论：
1. **生产权重**在每折留出段的超额收益与 t 值（这是真正上线的方案）；
2. **每折训练段最优方案**在留出段的表现 —— 如果它普遍不如生产权重，
   说明「调参能跑赢」只是拟合噪声（这正是之前 240 天窗口得出的结论）；
3. **随机对照噪声带**：同一套脚手架下随机选股的 t 值区间，用来判断
   「某个 t 值」是否只是运气。

用法::

    # 用现有缓存（.cache/walk_forward_1400.pkl）跑，没有就现建
    python scripts/walk_forward.py --reuse

    # 指定折数与步长
    python scripts/walk_forward.py --train 250 --hold 60 --gap 10 --step 90 --max-folds 6

    # 只看最近 3 折，并且安静运行
    python scripts/walk_forward.py --max-folds 3 --quiet

已知偏差（不掩盖）
------------------
窗口重叠（同一折内相邻评估日的 k 日收益重叠，t 值偏高）、网格搜索的多重检验、
股票池快照只有「当前」一份（幸存者偏差，除非先用 ``backfill_universe.py``
补出历史时点快照）。

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

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# 必须在 import app.* 之前切到 backend：SQLAlchemy 把 sqlite:///./a_stock.db
# 解析成相对路径，晚一步就会连到项目根目录的空库。
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT / "scripts"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import param_search as ps  # noqa: E402
from app.realtime.param_search import (  # noqa: E402
    Book,
    build_cache,
    build_folds,
    noise_stats,
    slice_payload,
)
from app.realtime.screener import ScreenerConfig  # noqa: E402

H_PRIMARY = ps.H_PRIMARY
PROD_MIN_TRIGGERS = ScreenerConfig().min_triggers
PROD_TOP_N = ScreenerConfig().top_n


def find_row(results: list[dict], name: str) -> dict | None:
    for row in results:
        if row["name"] == name:
            return row
    return None


def _fmt_band(noise: dict) -> str:
    if noise.get("min") is None:
        return "[无随机对照样本]"
    return f"t 区间 [{noise['min']:.2f}, {noise['max']:.2f}](n={noise['n']})"


def fold_verdict(prod: dict, best: dict, noise: dict) -> str:
    """一折的定性结论；噪声带用于判断「训练最优」是不是运气。"""
    prod_t = prod[f"{H_PRIMARY}"]["t"]
    best_t = best[f"{H_PRIMARY}"]["t"]
    band_max = noise.get("max")
    tag = ""
    if band_max is not None and best_t > 0:
        tag = (
            "（训练最优 t 仍在噪声带内，无统计意义）"
            if best_t <= band_max
            else "（训练最优 t 超出噪声带上沿）"
        )
    if prod_t > 0 and best_t > 0:
        return "生产与训练最优在留出段同为正" + tag
    if prod_t > 0 >= best_t:
        return "生产为正、训练最优为负：调参在拟合噪声"
    if prod_t <= 0 < best_t:
        return "生产为负、训练最优为正：谨慎，可能是单折运气" + tag
    return "两者在留出段皆不为正"


def run_fold(payload: dict, lo: int, hi: int, args: argparse.Namespace, log) -> dict:
    sliced = slice_payload(payload, lo, hi)
    book = Book(sliced)
    report = ps.run_search(book, args.train, args.hold, log=log)
    results = report["results"]
    prod_name = f"grid:生产:min{PROD_MIN_TRIGGERS}:top{PROD_TOP_N}"
    prod = find_row(results, prod_name)
    if prod is None:  # 生产组合不在网格里时退回到等权对照，避免整折作废
        prod = find_row(results, f"grid:等权:min{PROD_MIN_TRIGGERS}:top{PROD_TOP_N}")
        prod_name += "(缺失，退回等权)"
    best = report["best_on_train"]
    row = {
        "train_span": report["train_span"],
        "hold_span": report["hold_span"],
        "purge_days": report["purge_days"],
        "production_name": prod_name,
        "production_hold": prod["hold"] if prod else None,
        "production_train": prod["train"] if prod else None,
        "best_on_train": {
            "name": best["name"],
            "weights": best["weights"],
            "min_triggers": best["min_triggers"],
            "top_n": best["top_n"],
            "train": best["train"],
            "hold": best["hold"],
        },
        # run_search 把随机对照放在 report["noise_band"]，逐盐值留出 t 现算分布
        "noise_hold_t": noise_stats(
            (report.get("noise_band") or {}).get("hold_t_values") or []
        ),
        "top_bottom_spread_hold": report["top_bottom_spread"]["生产权重"]["hold"],
    }
    row["verdict"] = fold_verdict(
        row["production_hold"] or {str(H_PRIMARY): {"t": 0.0}},
        best["hold"],
        row["noise_hold_t"],
    )
    return row


def summarise(folds: list[dict]) -> dict:
    """把多折结果压成一句话能读的结论（不做任何美化）。"""
    h = str(H_PRIMARY)
    prod_excess = [
        f["production_hold"][h]["excess_pct"] for f in folds if f["production_hold"]
    ]
    prod_t = [f["production_hold"][h]["t"] for f in folds if f["production_hold"]]
    best_excess = [f["best_on_train"]["hold"][h]["excess_pct"] for f in folds]
    best_t = [f["best_on_train"]["hold"][h]["t"] for f in folds]
    best_is_prod = [
        f["best_on_train"]["name"] == f["production_name"] for f in folds
    ]
    if not prod_excess:
        return {"folds": len(folds), "note": "没有任何一折拿到生产组合，无法汇总"}
    return {
        "folds": len(folds),
        "production_excess_mean_pct": round(float(np.mean(prod_excess)), 3),
        "production_excess_positive_folds": int(sum(1 for v in prod_excess if v > 0)),
        "production_excess_min_pct": round(float(min(prod_excess)), 3),
        "production_excess_max_pct": round(float(max(prod_excess)), 3),
        "production_t_mean": round(float(np.mean(prod_t)), 2),
        "production_t_positive_folds": int(sum(1 for v in prod_t if v > 0)),
        "best_on_train_excess_mean_pct": round(float(np.mean(best_excess)), 3),
        "best_on_train_excess_positive_folds": int(sum(1 for v in best_excess if v > 0)),
        "best_on_train_t_mean": round(float(np.mean(best_t)), 2),
        "best_on_train_is_production_folds": int(sum(1 for v in best_is_prod if v)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="跨年份 walk-forward 样本外验证")
    parser.add_argument("--eval-days", type=int, default=1400, help="整段缓存窗口，默认 1400")
    parser.add_argument("--end-day", default="", help="整段窗口右端（YYYY-MM-DD），缺省到最后一天")
    parser.add_argument("--train", type=int, default=250, help="每折训练段评估日数，默认 250")
    parser.add_argument("--hold", type=int, default=60, help="每折留出段评估日数，默认 60")
    parser.add_argument("--gap", type=int, default=10, help="隔离带交易日数，默认 10")
    parser.add_argument("--step", type=int, default=90, help="折间滚动步长，默认 90")
    parser.add_argument("--max-folds", type=int, default=6, help="最多几折，默认 6")
    parser.add_argument("--cache", default="", help="缓存路径，默认 <项目根>/.cache/walk_forward_<窗口>.pkl")
    parser.add_argument("--reuse", action="store_true", help="复用已有缓存")
    parser.add_argument("--quiet", action="store_true", help="不打印每折的详细日志")
    parser.add_argument("--out", default="", help="报告输出，默认 outputs/walk_forward_<时间戳>.json")
    args = parser.parse_args()

    log = (lambda *a, **k: None) if args.quiet else print
    end_day = date.fromisoformat(args.end_day) if args.end_day else None
    suffix = f"_{end_day.isoformat()}" if end_day else ""
    cache_path = Path(args.cache) if args.cache else (
        ROOT / ".cache" / f"walk_forward_{args.eval_days}{suffix}.pkl"
    )

    started = time.perf_counter()
    if args.reuse and cache_path.exists():
        print(f"复用缓存 {cache_path}")
        with cache_path.open("rb") as fh:
            payload = pickle.load(fh)
    else:
        payload = build_cache(args.eval_days, log=print, end_day=end_day)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as fh:
            pickle.dump(payload, fh, protocol=5)
        print(f"缓存已写入 {cache_path}（{cache_path.stat().st_size / 1e6:.1f}MB，"
              f"累计 {time.perf_counter() - started:.0f}s）")

    dates = [rec["date"] for rec in payload["days"]]
    if not dates:
        raise SystemExit("缓存为空，无法做 walk-forward")
    folds_idx = build_folds(
        len(dates), args.train, args.gap, args.hold, args.step, args.max_folds
    )
    if not folds_idx:
        raise SystemExit(
            f"缓存只有 {len(dates)} 个评估日，装不下 train={args.train}+gap={args.gap}"
            f"+hold={args.hold}"
        )
    print(f"\n缓存 {len(dates)} 个评估日（{dates[0]} ~ {dates[-1]}，复权 {payload['adjust']}，"
          f"股票池 {payload.get('universe')} 只，快照 {payload.get('snapshot_day')}）")
    print(f"共 {len(folds_idx)} 折；每折 train={args.train} / gap={args.gap} / hold={args.hold}\n")

    folds: list[dict] = []
    for i, (lo, hi) in enumerate(folds_idx, start=1):
        print(f"--- 第 {i}/{len(folds_idx)} 折：{dates[lo]} ~ {dates[hi]} ---")
        fold = run_fold(payload, lo, hi, args, log)
        h = str(H_PRIMARY)
        if fold["production_hold"]:
            print(f"    生产权重    留出 超额 {fold['production_hold'][h]['excess_pct']:>7.3f}%"
                  f"  t={fold['production_hold'][h]['t']:>5.2f}"
                  f"  命中率 {fold['production_hold'][h]['hit_rate']:.3f}"
                  f"  n={fold['production_hold'][h]['n']}")
        print(f"    训练最优    留出 超额 {fold['best_on_train']['hold'][h]['excess_pct']:>7.3f}%"
              f"  t={fold['best_on_train']['hold'][h]['t']:>5.2f}"
              f"  ({fold['best_on_train']['name']})")
        print(f"    噪声带(留出) {_fmt_band(fold['noise_hold_t'])}   {fold['verdict']}\n")
        folds.append(fold)

    summary = summarise(folds)
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cache_file": str(cache_path),
        "eval_days": len(dates),
        "span": [dates[0], dates[-1]],
        "bars_adjust": payload["adjust"],
        "universe_size": payload.get("universe"),
        "symbol_snapshot_day": payload.get("snapshot_day"),
        "train_days": args.train,
        "hold_days": args.hold,
        "purge_days": args.gap,
        "step_days": args.step,
        "primary_horizon": H_PRIMARY,
        "production_config": {"min_triggers": PROD_MIN_TRIGGERS, "top_n": PROD_TOP_N},
        "summary": summary,
        "folds": folds,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "caveats": [
            "窗口重叠：同一折内相邻评估日的 k 日收益互相重叠，t 统计量按独立样本读会偏高。",
            "多重检验：每折都在训练段挑最优，fold 数越多越容易挑到运气好的方案。",
            "幸存者偏差：股票池快照只有一份「当前」名单，历史退市标的不在样本内"
            "（除非先用 scripts/backfill_universe.py 补历史时点快照）。",
            "分析结果仅用于研究，不构成投资建议。",
        ],
    }
    out = Path(args.out) if args.out else (
        ROOT / "outputs" / f"walk_forward_{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("=== 汇总（主口径 %d 日超额，相对同池等权基准）===" % H_PRIMARY)
    print(f"折数 {summary['folds']}；生产权重留出超额均值 "
          f"{summary['production_excess_mean_pct']}%，"
          f"{summary['production_excess_positive_folds']}/{summary['folds']} 折为正；"
          f"t 均值 {summary['production_t_mean']}")
    print(f"训练最优留出超额均值 {summary['best_on_train_excess_mean_pct']}%，"
          f"{summary['best_on_train_excess_positive_folds']}/{summary['folds']} 折为正；"
          f"t 均值 {summary['best_on_train_t_mean']}；"
          f"训练段选中生产权重的折数 {summary['best_on_train_is_production_folds']}")
    print(f"\n报告：{out}")
    print("分析结果仅用于研究，不构成投资建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
