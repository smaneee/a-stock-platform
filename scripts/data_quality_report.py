"""数据质量报告生成器（研发计划 P0-03 / 5.1 节「数据质量日报」）。

只读打开主库（``mode=ro``），逐项检查并输出：

* ``outputs/quality/data-quality-<YYYYMMDD>.json``：机器可读全量结果
* ``outputs/quality/data-quality-<YYYYMMDD>.md``：人读报告

检查项与验收口径：

1. **股票池分市场验收**：沪深京分别给出总数 / 在册 / 剔除 / 状态分布，缺任一市场
   必须显式标注为「覆盖不足」，不得冒充全 A。
2. **point-in-time 快照**：快照数量、日期范围；历史不完整时明确拒绝「完整历史快照」。
3. **日线质量**：复权口径分布、按市场覆盖、重复行、非法价格（<=0 / high<low /
   high<max(o,c) / low>min(o,c)）、近 N 个交易日的逐标的缺口。
4. **涨跌停情绪池**：来源分布（eastmoney / derived）、覆盖标的数、日期范围、
   算法版本字段是否存在（缺失即视为不可审计）。
5. **证券主数据**：上市/退市日期缺失率、状态分布。

用法:
    python scripts/data_quality_report.py [--db PATH] [--outdir DIR] [--gap-days 20]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "backend" / "a_stock.db"

EXCHANGE_EXPECTED = {"SH": "上交所", "SZ": "深交所", "BJ": "北交所"}

# 与 exclusion 模块保持一致的市场划分口径（仅用于报告展示）
def exchange_of(symbol: str) -> str:
    raw = (symbol or "").strip().lower()
    if raw.startswith("bj"):
        return "BJ"
    if raw.startswith("sh"):
        return "SH"
    if raw.startswith("sz"):
        return "SZ"
    if raw.startswith(("4", "8")) or raw.startswith("92"):
        return "BJ"
    if raw.startswith(("6", "5", "9")):
        return "SH"
    return "SZ"


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + str(path).replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=60)
    if con.execute("pragma page_count").fetchone()[0] == 0:
        con.close()
        raise RuntimeError("page_count==0，URI 解析错误，拒绝继续")
    return con


def check_universe(con: sqlite3.Connection) -> dict:
    latest = con.execute(
        "select id, trading_day, total_count, included_count, excluded_count, source_provider, created_at"
        " from universe_snapshots order by trading_day desc limit 1"
    ).fetchone()
    snapshots = con.execute("select count(*), min(trading_day), max(trading_day) from universe_snapshots").fetchone()
    result: dict = {
        "snapshot_count": snapshots[0],
        "snapshot_range": [snapshots[1], snapshots[2]],
        "latest_snapshot": None,
        "by_exchange": {},
        "by_status": {},
        "exclusion_reasons": {},
        "markets_missing": [],
        "verdict": "unknown",
    }
    if latest is None:
        result["verdict"] = "FAIL：没有任何股票池快照"
        return result
    snap_id, day, total, included, excluded, provider, created = latest
    result["latest_snapshot"] = {
        "id": snap_id,
        "trading_day": day,
        "total_count": total,
        "included_count": included,
        "excluded_count": excluded,
        "source_provider": provider,
        "created_at": created,
    }
    for symbol, is_included, status, reason in con.execute(
        "select symbol, is_included, trading_status, exclude_reason from universe_members where snapshot_id=?",
        (snap_id,),
    ):
        ex = exchange_of(symbol)
        bucket = result["by_exchange"].setdefault(
            ex, {"total": 0, "included": 0, "excluded": 0}
        )
        bucket["total"] += 1
        bucket["included" if is_included else "excluded"] += 1
        key = (status or "unknown").strip() or "unknown"
        result["by_status"][key] = result["by_status"].get(key, 0) + 1
        if not is_included:
            reason_key = (reason or "unspecified").strip() or "unspecified"
            result["exclusion_reasons"][reason_key] = result["exclusion_reasons"].get(reason_key, 0) + 1
    result["markets_missing"] = [ex for ex in EXCHANGE_EXPECTED if ex not in result["by_exchange"]]
    if result["markets_missing"]:
        result["verdict"] = (
            "WARN：快照缺少市场 " + ",".join(result["markets_missing"]) +
            "；只能声明为『沪深范围研究』，禁止冒充全 A"
        )
    else:
        result["verdict"] = "PASS：沪深京三市场均有成员"
    return result


def check_bars(con: sqlite3.Connection, gap_days: int) -> dict:
    out: dict = {}
    adjust_rows = con.execute(
        "select adjust, count(*), count(distinct symbol), min(trade_date), max(trade_date)"
        " from historical_bars group by adjust order by adjust"
    ).fetchall()
    out["by_adjust"] = [
        {"adjust": r[0], "rows": r[1], "symbols": r[2], "min_date": r[3], "max_date": r[4]}
        for r in adjust_rows
    ]
    out["by_period"] = [
        {"period": r[0], "rows": r[1]}
        for r in con.execute("select period, count(*) from historical_bars group by period").fetchall()
    ]
    # 重复行（唯一索引应保证 0；这里显式验证而不是假设）
    dup = con.execute(
        "select count(*) from (select symbol, period, adjust, trade_date, count(*) c"
        " from historical_bars group by symbol, period, adjust, trade_date having c > 1)"
    ).fetchone()[0]
    out["duplicate_groups"] = int(dup)

    # 非法价格（只查未复权日线，避免复权因子放大的边界噪声）
    bad = con.execute(
        "select"
        " sum(case when close <= 0 or open <= 0 then 1 else 0 end),"
        " sum(case when high < low then 1 else 0 end),"
        " sum(case when high < open or high < close then 1 else 0 end),"
        " sum(case when low > open or low > close then 1 else 0 end)"
        " from historical_bars where period='daily' and adjust='none'"
    ).fetchone()
    out["invalid"] = {
        "non_positive_price": int(bad[0] or 0),
        "high_lt_low": int(bad[1] or 0),
        "high_lt_open_or_close": int(bad[2] or 0),
        "low_gt_open_or_close": int(bad[3] or 0),
    }
    out["source_distribution"] = [
        {"source": r[0] or "unknown", "rows": r[1]}
        for r in con.execute(
            "select source, count(*) from historical_bars where period='daily' group by source order by 2 desc"
        ).fetchall()
    ]

    # 最近 gap_days 个交易日的逐标的覆盖（按市场汇总 + 最差标的）
    days = [r[0] for r in con.execute(
        "select distinct trade_date from historical_bars where period='daily' and adjust='none'"
        " order by trade_date desc limit ?", (gap_days,)
    ).fetchall()]
    out["gap_window_days"] = days
    if days:
        placeholders = ",".join("?" for _ in days)
        rows = con.execute(
            f"select symbol, count(distinct trade_date) c from historical_bars"
            f" where period='daily' and adjust='none' and trade_date in ({placeholders})"
            f" group by symbol",
            days,
        ).fetchall()
        full = sum(1 for _s, c in rows if c == len(days))
        partial = sum(1 for _s, c in rows if 0 < c < len(days))
        worst = sorted(rows, key=lambda r: r[1])[:20]
        out["gap_summary"] = {
            "symbols_total": len(rows),
            "symbols_full_coverage": full,
            "symbols_partial_coverage": partial,
            "worst_symbols": [{"symbol": s, "days": c} for s, c in worst],
        }
        # 在册标的里完全没有日线的
        latest_snap = con.execute(
            "select id from universe_snapshots order by trading_day desc limit 1"
        ).fetchone()
        if latest_snap:
            missing = con.execute(
                f"select count(*) from universe_members m where m.snapshot_id=?"
                f" and m.is_included=1 and m.symbol not in ("
                f"  select distinct symbol from historical_bars where period='daily' and adjust='none'"
                f"    and trade_date in ({placeholders}))",
                [latest_snap[0], *days],
            ).fetchone()[0]
            out["gap_summary"]["included_symbols_without_recent_bars"] = int(missing)
    return out


def check_limit_up(con: sqlite3.Connection) -> dict:
    cols = [c[1] for c in con.execute("pragma table_info(limit_up_sentiment)")]
    out: dict = {
        "columns": cols,
        "has_algorithm_version": "algorithm_version" in cols,
        "by_source": [
            {"source": r[0] or "unknown", "rows": r[1], "min_date": r[2], "max_date": r[3],
             "min_coverage": r[4], "max_coverage": r[5]}
            for r in con.execute(
                "select source, count(*), min(trade_date), max(trade_date),"
                " min(coverage_symbols), max(coverage_symbols)"
                " from limit_up_sentiment group by source order by 2 desc"
            ).fetchall()
        ],
    }
    if "algorithm_version" in cols:
        out["by_source_and_version"] = [
            {"source": r[0] or "unknown", "algorithm_version": r[1] or "未登记",
             "rows": r[2], "min_date": r[3], "max_date": r[4],
             "min_coverage": r[5], "max_coverage": r[6]}
            for r in con.execute(
                "select source, algorithm_version, count(*), min(trade_date), max(trade_date),"
                " min(coverage_symbols), max(coverage_symbols)"
                " from limit_up_sentiment group by source, algorithm_version order by 3 desc"
            ).fetchall()
        ]
        out["rows_without_version"] = int(con.execute(
            "select count(*) from limit_up_sentiment where algorithm_version is null"
        ).fetchone()[0])
        out["eastmoney_rows_without_coverage"] = int(con.execute(
            "select count(*) from limit_up_sentiment"
            " where source='eastmoney' and coverage_symbols is null"
        ).fetchone()[0])
    pool = con.execute(
        "select count(*), count(distinct trade_date), count(distinct symbol), min(trade_date), max(trade_date)"
        " from limit_up_pool_members"
    ).fetchone()
    out["pool_members"] = {
        "rows": pool[0], "days": pool[1], "symbols": pool[2],
        "min_date": pool[3], "max_date": pool[4],
    }
    return out


def check_securities(con: sqlite3.Connection) -> dict:
    total = con.execute("select count(*) from securities").fetchone()[0]
    missing_listing = con.execute("select count(*) from securities where listing_date is null").fetchone()[0]
    missing_delisted = con.execute("select count(*) from securities where delisted_date is null").fetchone()[0]
    status = [
        {"status": r[0] or "unknown", "count": r[1]}
        for r in con.execute("select trading_status, count(*) from securities group by trading_status order by 2 desc")
    ]
    st = con.execute("select count(*) from securities where is_st=1").fetchone()[0]
    return {
        "total": total,
        "missing_listing_date": missing_listing,
        "missing_listing_ratio": round(missing_listing / total, 4) if total else None,
        "delisted_without_date": missing_delisted,
        "is_st": st,
        "by_status": status,
    }


def render_markdown(payload: dict) -> str:
    u = payload["universe"]
    b = payload["bars"]
    lu = payload["limit_up"]
    sec = payload["securities"]
    lines = [
        f"# 数据质量报告 · {payload['generated_at'][:19]}",
        "",
        f"- 数据库：`{payload['db_path']}`（只读打开）",
        f"- 生成命令：`{payload['command']}`",
        "- 免责声明：所有分析结果仅用于研究，不构成投资建议。",
        "",
        "## 1. 股票池（分市场验收）",
        "",
        f"- 快照数量：**{u['snapshot_count']}**，范围 {u['snapshot_range'][0]} ~ {u['snapshot_range'][1]}",
    ]
    if u["latest_snapshot"]:
        ls = u["latest_snapshot"]
        lines += [
            f"- 最新快照：`{ls['trading_day']}`（provider={ls['source_provider']}，"
            f"total={ls['total_count']}，included={ls['included_count']}，excluded={ls['excluded_count']}）",
            "",
            "| 市场 | 总数 | 在册 | 剔除 |",
            "| --- | --- | --- | --- |",
        ]
        for ex, label in EXCHANGE_EXPECTED.items():
            bucket = u["by_exchange"].get(ex, {"total": 0, "included": 0, "excluded": 0})
            lines.append(f"| {label}（{ex}） | {bucket['total']} | {bucket['included']} | {bucket['excluded']} |")
        lines += [
            "",
            f"- 缺失市场：{u['markets_missing'] or '无'}",
            f"- 状态分布：{json.dumps(u['by_status'], ensure_ascii=False)}",
            f"- 剔除原因：{json.dumps(u['exclusion_reasons'], ensure_ascii=False)}",
            f"- **判定：{u['verdict']}**",
            "",
        ]
    lines += [
        "## 2. 日线质量",
        "",
        f"- 复权口径分布：{json.dumps(b['by_adjust'], ensure_ascii=False)}",
        f"- 周期分布：{json.dumps(b['by_period'], ensure_ascii=False)}",
        f"- 重复（symbol, period, adjust, trade_date）组数：**{b['duplicate_groups']}**（唯一索引应保证 0）",
        f"- 非法价格：{json.dumps(b['invalid'], ensure_ascii=False)}",
        f"- 来源分布：{json.dumps(b['source_distribution'], ensure_ascii=False)}",
        "",
    ]
    if b.get("gap_summary"):
        g = b["gap_summary"]
        lines += [
            f"- 近 {len(b['gap_window_days'])} 个交易日（{b['gap_window_days'][-1]} ~ {b['gap_window_days'][0]}）覆盖："
            f"有行情标的 {g['symbols_total']}，全覆盖 {g['symbols_full_coverage']}，"
            f"部分覆盖 {g['symbols_partial_coverage']}",
            f"- 在册但近窗口无任何日线的标的：**{g.get('included_symbols_without_recent_bars', 'n/a')}**",
            f"- 缺口最大标的（前 20）：{json.dumps(g['worst_symbols'][:10], ensure_ascii=False)}",
            "",
        ]
    lines += [
        "## 3. 涨停情绪池",
        "",
        f"- 是否含算法版本字段：**{lu['has_algorithm_version']}**",
        f"- 按来源：{json.dumps(lu['by_source'], ensure_ascii=False)}",
    ]
    if lu.get("by_source_and_version") is not None:
        lines += [
            f"- 按来源+算法版本：{json.dumps(lu['by_source_and_version'], ensure_ascii=False)}",
            f"- 未登记算法版本的行：**{lu['rows_without_version']}**",
            f"- 东财来源但缺 coverage_symbols 的行：**{lu['eastmoney_rows_without_coverage']}**",
        ]
    lines += [
        f"- 情绪池成员：{json.dumps(lu['pool_members'], ensure_ascii=False)}",
        "",
        "## 4. 证券主数据",
        "",
        f"- 总数 {sec['total']}，ST {sec['is_st']} 只",
        f"- 上市日期缺失：{sec['missing_listing_date']}（{sec['missing_listing_ratio']}）",
        f"- 状态分布：{json.dumps(sec['by_status'], ensure_ascii=False)}",
        "",
        "## 5. 结论与限制",
        "",
        payload["conclusion"],
        "",
    ]
    return "\n".join(lines)


def build_conclusion(payload: dict) -> str:
    notes: list[str] = []
    u = payload["universe"]
    if u["snapshot_count"] < 20:
        notes.append(
            f"- 历史快照仅 {u['snapshot_count']} 份：**不允许声明『完整历史股票池』**，"
            "任何跨期回测都必须声明幸存者偏差风险，或改用 point-in-time 快照逐日取池。"
        )
    if u["markets_missing"]:
        notes.append(
            f"- 最新快照缺市场 {','.join(u['markets_missing'])}：只能声明为『沪深范围研究』。"
        )
    if u["latest_snapshot"] and u["latest_snapshot"]["excluded_count"]:
        notes.append(
            f"- 剔除 {u['latest_snapshot']['excluded_count']} 只；剔除不等于停牌，"
            "能否成交必须看 `/api/market/session` 的 `tradable_now`。"
        )
    inv = payload["bars"]["invalid"]
    if any(inv.values()):
        notes.append(f"- 日线存在非法价格 {inv}：相关标的不得进入回测样本。")
    if not payload["limit_up"]["has_algorithm_version"]:
        notes.append(
            "- 涨停情绪表缺少 `algorithm_version` 字段：`derived`（日线回算）与 `eastmoney`（实抓）"
            "无法按算法版本区分，历史可比性不足（已列为待补 schema 变更）。"
        )
    else:
        lu = payload["limit_up"]
        if lu.get("rows_without_version"):
            notes.append(f"- 有 {lu['rows_without_version']} 行情绪数据未登记算法版本。")
        if lu.get("eastmoney_rows_without_coverage"):
            notes.append(
                f"- 有 {lu['eastmoney_rows_without_coverage']} 行东财情绪数据缺 `coverage_symbols`："
                "封板率的样本面无法说明（新版抓取已补齐，历史行需重抓或标注）。"
            )
    sec = payload["securities"]
    if sec["missing_listing_ratio"] and sec["missing_listing_ratio"] > 0.02:
        notes.append(
            f"- 上市日期缺失率 {sec['missing_listing_ratio']}：上市初期无涨跌停限制等边界规则会受影响。"
        )
    if not notes:
        notes.append("- 未发现阻断性问题；仍需按交易日持续累积快照与情绪数据。")
    return "\n".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--outdir", default=str(ROOT / "outputs" / "quality"))
    ap.add_argument("--gap-days", type=int, default=20)
    args = ap.parse_args()

    db = Path(args.db)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    con = connect_ro(db)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "db_path": str(db),
        "command": f"python scripts/data_quality_report.py --db \"{db}\" --gap-days {args.gap_days}",
        "universe": check_universe(con),
        "bars": check_bars(con, args.gap_days),
        "limit_up": check_limit_up(con),
        "securities": check_securities(con),
    }
    con.close()
    payload["conclusion"] = build_conclusion(payload)

    stamp = datetime.now().strftime("%Y%m%d")
    json_path = outdir / f"data-quality-{stamp}.json"
    md_path = outdir / f"data-quality-{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print("股票池:", payload["universe"]["verdict"])
    print("快照数:", payload["universe"]["snapshot_count"], "缺失市场:", payload["universe"]["markets_missing"])
    print("重复组:", payload["bars"]["duplicate_groups"], "非法价格:", payload["bars"]["invalid"])
    if payload["bars"].get("gap_summary"):
        g = payload["bars"]["gap_summary"]
        print(f"缺口窗口: 标的 {g['symbols_total']}，全覆盖 {g['symbols_full_coverage']}，"
              f"在册无日线 {g.get('included_symbols_without_recent_bars')}")
    print("情绪来源:", payload["limit_up"]["by_source"])
    print("写入:", json_path.name, md_path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
