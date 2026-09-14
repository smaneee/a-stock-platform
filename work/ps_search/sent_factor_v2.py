"""P0-01 情绪因子验证器 v2（修正口径的正式研究脚本）。

与旧 ``work/ps_search/sent_factor.py``（已不在工作区，见
``legacy/legacy-preservation-manifest.json``）的关系：

- 旧脚本与旧产物一律**不动**；本脚本只写 ``work/ps_search/out/`` 下的新文件。
- 口径完全不同（见 :mod:`sent_factor_core` 模块说明），报告里必须并列展示。

主库 ``backend/a_stock.db`` 一律以 ``file:...?mode=ro`` 只读打开，
并额外执行 ``PRAGMA query_only = ON``，任何写操作都会被 SQLite 拒绝。

用法::

    <venv-311>\\Scripts\\python.exe work\\ps_search\\sent_factor_v2.py \
        --db backend\\a_stock.db --out work\\ps_search\\out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sent_factor_core import (  # noqa: E402
    InsufficientSampleError,
    SourceBlock,
    SplitPlan,
    assert_no_lookahead,
    block_bootstrap_mean_ci,
    cross_check_spearman,
    ensure_sorted_unique_dates,
    hac_mean_ci,
    lookahead_violations,
    make_splits,
    monotonic_dates,
    sensitivity_table,
    spearman_sensitivity,
    split_by_source,
    split_integrity_report,
)

ALGO_VERSION = "sent-factor-v2.0.0"
DEFAULT_K = (1, 3, 5)
DEFAULT_ADJUSTS = ("qfq", "none")
# 低于该交易日数就不给方向性结论（只是「不确定」）
MIN_DAYS_FOR_DIRECTION = 120
SIGNAL_FIELDS = ("seal_rate", "broken_rate", "max_streak", "limit_up_count")


# ---------------------------------------------------------------------------
# 数据库（只读）
# ---------------------------------------------------------------------------


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开主库；不写、不建、不迁移。"""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_sentiment_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """取情绪序列。

    刻意**不**在 SQL 里依赖隐式顺序假设：这里虽然写了 ORDER BY（因为明确排序
    才是正确做法），但返回后仍强制走 :func:`ensure_sorted_unique_dates`，
    并记录「数据库原始返回顺序是否已升序」作为证据。
    """
    raw = conn.execute(
        """
        SELECT trade_date, source, seal_rate, broken_rate, max_streak,
               limit_up_count, limit_down_count, broken_board_count,
               coverage_symbols, captured_at
        FROM limit_up_sentiment
        ORDER BY trade_date ASC
        """
    ).fetchall()
    rows = [dict(r) for r in raw]
    was_sorted = monotonic_dates(rows)
    for row in rows:
        row["_db_order_monotonic"] = was_sorted
    return ensure_sorted_unique_dates(rows)


def fetch_calendar(conn: sqlite3.Connection, lo: str, hi: str) -> list[str]:
    """交易日历（显式升序）。"""
    cur = conn.execute(
        """
        SELECT trade_date FROM trading_calendar
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (lo, hi),
    )
    days = [str(r["trade_date"])[:10] for r in cur.fetchall()]
    if len(set(days)) != len(days):
        raise ValueError("交易日历存在重复交易日")
    return sorted(days)


def fetch_bars(
    conn: sqlite3.Connection, *, adjust: str, lo: str, hi: str
) -> pd.DataFrame:
    """取日线（显式 ORDER BY trade_date, symbol）。"""
    frame = pd.read_sql_query(
        """
        SELECT symbol, trade_date, open, close, high
        FROM historical_bars
        WHERE period = 'daily' AND adjust = ?
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC, symbol ASC
        """,
        conn,
        params=(adjust, lo, hi),
    )
    frame["trade_date"] = frame["trade_date"].astype(str).str.slice(0, 10)
    for col in ("open", "close", "high"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


# ---------------------------------------------------------------------------
# 纯计算：前瞻收益
# ---------------------------------------------------------------------------


def build_forward_returns(
    bars: pd.DataFrame, calendar: Sequence[str], k: int
) -> pd.DataFrame:
    """构造「信号日 t → t+1 开盘买入 → t+k 收盘卖出」的逐股收益。

    同时给出「t+1 收盘 → t+k 收盘」的备选口径（``ret_cc``），用于在只有收盘价
    的数据上说明可执行性限制。

    返回列：``sig_pos`` / ``sig_date`` / ``entry_date`` / ``exit_date`` /
    ``symbol`` / ``ret`` / ``ret_cc``。
    """
    if k < 1:
        raise ValueError("k 必须 >= 1")
    cal_index = {day: i for i, day in enumerate(calendar)}
    frame = bars[bars["trade_date"].isin(cal_index)].copy()
    frame["pos"] = frame["trade_date"].map(cal_index).astype(int)
    frame = frame.drop_duplicates(subset=["symbol", "pos"], keep="first")

    entry = frame[["symbol", "pos", "open", "close"]].copy()
    entry["sig_pos"] = entry["pos"] - 1
    entry = entry.rename(
        columns={"open": "entry_open", "close": "entry_close"}
    )[["symbol", "sig_pos", "entry_open", "entry_close"]]

    exit_frame = frame[["symbol", "pos", "close"]].copy()
    exit_frame["sig_pos"] = exit_frame["pos"] - k
    exit_frame = exit_frame.rename(columns={"close": "exit_close"})[
        ["symbol", "sig_pos", "exit_close"]
    ]

    merged = entry.merge(exit_frame, on=["symbol", "sig_pos"], how="inner")
    # sig_pos < 0 说明该行没有「前一交易日」可用于定义信号日，必须剔除；
    # 否则会拿日历末尾（负索引）冒充信号日，制造出「信号日晚于建仓日」的伪样本。
    merged = merged[merged["sig_pos"] >= 0]
    merged = merged[
        (merged["entry_open"] > 0)
        & (merged["exit_close"] > 0)
        & (merged["entry_close"] > 0)
    ].copy()
    merged["ret"] = merged["exit_close"] / merged["entry_open"] - 1.0
    merged["ret_cc"] = merged["exit_close"] / merged["entry_close"] - 1.0
    merged["sig_date"] = merged["sig_pos"].map(lambda p: calendar[int(p)])
    merged["entry_date"] = merged["sig_pos"].map(lambda p: calendar[int(p) + 1])
    merged["exit_date"] = merged["sig_pos"].map(lambda p: calendar[int(p) + k])
    # 最底层的未来函数守卫：任何「建仓日 <= 信号日」都直接失败，绝不进入后续统计。
    assert_no_lookahead(
        merged["sig_date"].tolist(),
        merged["entry_date"].tolist(),
        exit_dates=merged["exit_date"].tolist(),
    )
    return merged[
        ["sig_pos", "sig_date", "entry_date", "exit_date", "symbol", "ret", "ret_cc"]
    ].reset_index(drop=True)


def daily_market_series(per_symbol: pd.DataFrame, ret_col: str = "ret") -> pd.DataFrame:
    """把逐股收益按日聚合成等权组合收益（同日多只股票不是独立样本）。

    同时带出该信号日对应的 ``entry_date`` / ``exit_date``，保证「信号日 → 建仓日」
    的映射在聚合后依然可见，便于事后做未来函数审计。
    """
    grouped = per_symbol.groupby("sig_date", sort=True)
    out = grouped[ret_col].agg(mean="mean", n_symbols="count", median="median")
    out["entry_date"] = grouped["entry_date"].first()
    out["exit_date"] = grouped["exit_date"].first()
    out = out.reset_index().sort_values("sig_date").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# 研究主流程
# ---------------------------------------------------------------------------


def analyse_source(
    *,
    source: str,
    sentiment: pd.DataFrame,
    market: pd.DataFrame,
    horizon: int,
    plan: SplitPlan | None,
    integrity: dict[str, object] | None,
    captured_at: str,
    plan_failure_reason: str | None = None,
) -> dict[str, object]:
    """对单一来源做完整分析（禁止与其它来源混池）。"""
    block = {
        "source": source,
        "algo_version": ALGO_VERSION,
        "observed_available_at": captured_at,
        "horizon_k": horizon,
    }
    n_days = int(len(sentiment))
    block["coverage_days"] = n_days
    block["coverage_symbols_median"] = float(market["n_symbols"].median()) if len(market) else 0.0
    block["first_date"] = str(sentiment["trade_date"].iloc[0]) if n_days else None
    block["last_date"] = str(sentiment["trade_date"].iloc[-1]) if n_days else None

    merged = sentiment.merge(market, left_on="trade_date", right_on="sig_date", how="inner")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    aligned_days = int(len(merged))
    block["aligned_days_with_return"] = aligned_days

    if aligned_days < 3:
        block["verdict"] = "样本不足，结论为不确定"
        block["reason"] = f"可对齐的信号日仅 {aligned_days} 天，无法估计相关性"
        return block

    # --- 未来函数守卫（真实数据上的硬断言）---
    entry_dates = merged["entry_date"].tolist() if "entry_date" in merged else []
    block["lookahead_check"] = "n/a"
    if entry_dates:
        violations = lookahead_violations(merged["trade_date"].tolist(), entry_dates)
        block["lookahead_violations"] = len(violations)
        assert_no_lookahead(
            merged["trade_date"].tolist(),
            entry_dates,
            exit_dates=merged["exit_date"].tolist(),
        )
        block["lookahead_check"] = "pass"

    # --- Spearman（平均秩）+ scipy 交叉核对 ---
    block["spearman"] = {}
    for field in SIGNAL_FIELDS:
        series = pd.to_numeric(merged[field], errors="coerce")
        valid = series.notna() & merged["mean"].notna()
        if int(valid.sum()) < 3:
            block["spearman"][field] = {"n": int(valid.sum()), "note": "有效样本不足"}
            continue
        x = series[valid].to_numpy(dtype=float)
        y = merged.loc[valid, "mean"].to_numpy(dtype=float)
        check = cross_check_spearman(x, y, tol=1e-9)
        check["block_bootstrap"] = spearman_sensitivity(
            x, y, blocks=(1, max(horizon, 1), 5, 10, 20), n_boot=2000, seed=20260913
        )
        check["note_pvalue"] = (
            "scipy 的 p 值假设样本独立；持有 k 日时窗口重叠，该 p 值会高估显著性。"
            "判断是否稳健请用 block_bootstrap 的区间。"
        )
        block["spearman"][field] = check

    # --- 样本划分 ---
    if plan is None:
        block["verdict"] = "样本不足，结论为不确定"
        block["reason"] = plan_failure_reason or (
            f"可对齐信号日 {aligned_days} 天 < 建立训练/选择/隔离带/留出四段所需的"
            f"最低要求（{MIN_DAYS_FOR_DIRECTION}）"
        )
        block["splits"] = None
        return block

    block["splits"] = plan.as_dict()
    block["split_integrity"] = integrity

    # --- 时序择时：阈值只在训练段确定 ---
    train = merged[merged["trade_date"].isin(plan.train.dates)]
    if len(train) < 10:
        block["verdict"] = "样本不足，结论为不确定"
        block["reason"] = f"训练段仅 {len(train)} 天"
        return block
    threshold = float(pd.to_numeric(train["seal_rate"], errors="coerce").median())
    block["timing_rule"] = {
        "signal": "seal_rate",
        "threshold": threshold,
        "threshold_source": "train split median only (冻结，不在留出段重估)",
        "position": "w=1 若 seal_rate_t >= threshold，否则 w=0（空仓/现金）",
        "execution": "t 日收盘后得信号，t+1 开盘建仓（k=1 时 t+1 收盘平仓）",
    }

    merged = merged.copy()
    merged["w"] = (pd.to_numeric(merged["seal_rate"], errors="coerce") >= threshold).astype(float)
    merged["spread"] = (2.0 * merged["w"] - 1.0) * merged["mean"]
    merged["overlay_excess"] = (merged["w"] - 1.0) * merged["mean"]

    seg_results: dict[str, object] = {}
    for name, split in (
        ("train", plan.train),
        ("select", plan.select),
        ("holdout", plan.holdout),
    ):
        seg = merged[merged["trade_date"].isin(split.dates)]
        if len(seg) < 10:
            seg_results[name] = {"n": int(len(seg)), "note": "样本不足"}
            continue
        seg_results[name] = {
            "n": int(len(seg)),
            "start": split.start,
            "end": split.end,
            "mean_market_ret": float(seg["mean"].mean()),
            "mean_spread": float(seg["spread"].mean()),
            "mean_overlay_excess_vs_buyhold": float(seg["overlay_excess"].mean()),
            "sensitivity_spread": sensitivity_table(
                seg["spread"].to_numpy(dtype=float),
                lags=(0, 1, 5, 10),
                blocks=(1, 5, 10, 20),
                n_boot=2000,
            ),
            "sensitivity_overlay_excess": sensitivity_table(
                seg["overlay_excess"].to_numpy(dtype=float),
                lags=(0, 1, 5, 10),
                blocks=(1, 5, 10, 20),
                n_boot=2000,
            ),
            "mean_market_ret_when_high": float(seg.loc[seg["w"] == 1.0, "mean"].mean())
            if (seg["w"] == 1.0).any()
            else None,
            "mean_market_ret_when_low": float(seg.loc[seg["w"] == 0.0, "mean"].mean())
            if (seg["w"] == 0.0).any()
            else None,
        }
    block["segments"] = seg_results

    holdout = seg_results.get("holdout", {})
    if int(holdout.get("n", 0)) < 10:
        block["verdict"] = "样本不足，结论为不确定"
        block["reason"] = "最终留出段样本不足"
    else:
        overlay_sens = holdout["sensitivity_overlay_excess"]
        ci_lows = [item["ci_low"] for item in overlay_sens["hac"]]
        ci_highs = [item["ci_high"] for item in overlay_sens["hac"]]
        stable_positive = all(lo > 0 for lo in ci_lows)
        stable_negative = all(hi < 0 for hi in ci_highs)
        if stable_positive:
            block["verdict"] = "留出段择时叠加相对同池买入持有为正（仍未扣除费用与滑点）"
        elif stable_negative:
            block["verdict"] = "留出段择时叠加相对同池买入持有为负"
        else:
            block["verdict"] = "区间跨越零，结论为不确定"
        block["verdict_note"] = (
            "本结果只是同池等权买入持有的择时叠加上下文，不衡量选股能力，"
            "也未扣除佣金/印花税/滑点/冲击成本；不构成可交易预测力证据。"
        )
    return block


def _sha256(path: Path, chunk: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-01 情绪因子验证器 v2")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--k", nargs="*", type=int, default=list(DEFAULT_K))
    parser.add_argument("--adjusts", nargs="*", default=list(DEFAULT_ADJUSTS))
    parser.add_argument("--hash-db", action="store_true", help="计算主库 SHA-256（耗时）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_ro(db_path)
    try:
        rows = fetch_sentiment_rows(conn)
        by_source = split_by_source(rows)
        lo = min(str(r["trade_date"]) for r in rows)
        hi = max(str(r["trade_date"]) for r in rows)
        # 前后各留缓冲，保证最后一次信号的 k 日收益也能取到
        hi_buf = (date.fromisoformat(hi) + timedelta(days=30)).isoformat()
        lo_buf = (date.fromisoformat(lo) - timedelta(days=20)).isoformat()
        calendar = fetch_calendar(conn, lo_buf, hi_buf)
        bars_by_adjust = {
            adjust: fetch_bars(conn, adjust=adjust, lo=lo_buf, hi=hi_buf)
            for adjust in args.adjusts
        }
        captured = conn.execute(
            "SELECT source, MIN(captured_at) cmin, MAX(captured_at) cmax FROM limit_up_sentiment GROUP BY source"
        ).fetchall()
    finally:
        conn.close()

    capture_map = {str(r["source"]): {"min": str(r["cmin"]), "max": str(r["cmax"])} for r in captured}

    payload: dict[str, object] = {
        "algo_version": ALGO_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "algo_notes": {
            "signal_availability": "情绪因子只在信号日 t 收盘后可得；建仓统一放在 t+1 开盘",
            "return_window_primary": "t+1 开盘 → t+k 收盘（本地日线有 open，故用开盘建仓）",
            "return_window_fallback": "t+1 收盘 → t+k 收盘（ret_cc），仅在只有收盘价时可用，可执行性更差",
            "source_policy": "derived 与 eastmoney 严禁混池，逐来源单独统计",
            "overlap_policy": "重叠 k 日窗口不得视为独立样本；日度聚合后用 HAC(Newey-West) 与移动块自助法给区间",
        },
        "db": {
            "path": str(db_path),
            "size_bytes": db_path.stat().st_size,
            "mtime": datetime.fromtimestamp(db_path.stat().st_mtime, timezone.utc).isoformat(),
            "opened_mode": "file:...?mode=ro + PRAGMA query_only=ON",
            "sha256": _sha256(db_path) if args.hash_db else None,
        },
        "sentiment": {
            "rows": len(rows),
            "db_return_order_monotonic": bool(rows[0]["_db_order_monotonic"]) if rows else None,
            "unique_dates_per_source": {
                src: len({str(r["trade_date"]) for r in items})
                for src, items in by_source.items()
            },
            "row_counts_per_source": {src: len(items) for src, items in by_source.items()},
            "captured_at_per_source": capture_map,
        },
        "calendar_days_in_window": len(calendar),
        "analyses": {},
    }

    for adjust in args.adjusts:
        bars = bars_by_adjust[adjust]
        payload["analyses"][adjust] = {}
        for k in args.k:
            per_symbol = build_forward_returns(bars, calendar, k)
            market = daily_market_series(per_symbol)
            payload["analyses"][adjust][f"k{k}"] = {
                "per_symbol_rows": int(len(per_symbol)),
                "market_days": int(len(market)),
                "median_universe_per_day": float(market["n_symbols"].median()),
                "ret_describe": {
                    "min": float(per_symbol["ret"].min()),
                    "max": float(per_symbol["ret"].max()),
                    "mean": float(per_symbol["ret"].mean()),
                    "abs_gt_1_count": int((per_symbol["ret"].abs() > 1.0).sum()),
                },
                "sources": {},
            }
            for src, items in by_source.items():
                sent = pd.DataFrame(
                    [
                        {
                            "trade_date": str(r["trade_date"]),
                            "seal_rate": r["seal_rate"],
                            "broken_rate": r["broken_rate"],
                            "max_streak": r["max_streak"],
                            "limit_up_count": r["limit_up_count"],
                            "coverage_symbols": r["coverage_symbols"],
                        }
                        for r in items
                    ]
                )
                aligned = sent.merge(market, left_on="trade_date", right_on="sig_date", how="inner")
                aligned = aligned.sort_values("trade_date").reset_index(drop=True)
                plan = None
                integrity = None
                plan_reason = None
                if len(aligned) >= MIN_DAYS_FOR_DIRECTION:
                    try:
                        plan = make_splits(
                            aligned["trade_date"].tolist(),
                            horizon=k,
                            train_frac=0.5,
                            select_frac=0.25,
                            min_holdout=30,
                        )
                        label_ends = dict(
                            zip(aligned["trade_date"], aligned["exit_date"])
                        )
                        integrity = split_integrity_report(plan, label_ends)
                    except InsufficientSampleError as exc:
                        plan = None
                        plan_reason = str(exc)
                        integrity = {"all_ok": False, "reason": str(exc)}
                cap = capture_map.get(src, {})
                block = analyse_source(
                    source=src,
                    sentiment=sent,
                    market=market,
                    horizon=k,
                    plan=plan,
                    integrity=integrity,
                    captured_at=f"{cap.get('min')} ~ {cap.get('max')}",
                    plan_failure_reason=plan_reason,
                )
                if plan is None and len(aligned) < MIN_DAYS_FOR_DIRECTION:
                    block.setdefault("verdict", "样本不足，结论为不确定")
                    block.setdefault(
                        "reason",
                        f"该来源可对齐信号日 {len(aligned)} 天 < {MIN_DAYS_FOR_DIRECTION}",
                    )
                payload["analyses"][adjust][f"k{k}"]["sources"][src] = block

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"sentiment-v2-{stamp}.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    latest = out_dir / "sentiment-v2-latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[ok] wrote {out_file}")
    print(json.dumps(_summary(payload), ensure_ascii=False, indent=2, default=str))
    return 0


def _summary(payload: dict[str, object]) -> dict[str, object]:
    """给终端用的精简摘要（完整结果在 JSON 文件里）。"""
    out: dict[str, object] = {
        "sentiment_rows": payload["sentiment"]["rows"],  # type: ignore[index]
        "sources": payload["sentiment"]["row_counts_per_source"],  # type: ignore[index]
        "analyses": {},
    }
    for adjust, by_k in payload["analyses"].items():  # type: ignore[union-attr]
        out["analyses"][adjust] = {}
        for k, block in by_k.items():
            entry = {"per_symbol_rows": block["per_symbol_rows"], "sources": {}}
            for src, sblock in block["sources"].items():
                sp = {
                    f: {
                        "self": round(v["self_value"], 6) if v.get("self_value") is not None else None,
                        "scipy": round(v["scipy_value"], 6) if v.get("scipy_value") is not None else None,
                        "ok": v.get("ok"),
                        "naive_p": round(v["scipy_pvalue"], 6) if v.get("scipy_pvalue") is not None else None,
                        "boot_robust_excludes_zero": (v.get("block_bootstrap") or {}).get(
                            "robust_excludes_zero"
                        ),
                        "boot_ci": [
                            [round(r["ci_low"], 6), round(r["ci_high"], 6)]
                            for r in (v.get("block_bootstrap") or {}).get("intervals", [])
                        ],
                    }
                    for f, v in (sblock.get("spearman") or {}).items()
                    if isinstance(v, dict) and "self_value" in v
                }
                entry["sources"][src] = {
                    "coverage_days": sblock.get("coverage_days"),
                    "aligned_days_with_return": sblock.get("aligned_days_with_return"),
                    "verdict": sblock.get("verdict"),
                    "spearman": sp,
                    "holdout_n": (sblock.get("segments") or {}).get("holdout", {}).get("n"),
                }
            out["analyses"][adjust][k] = entry
    return out


if __name__ == "__main__":
    raise SystemExit(main())
