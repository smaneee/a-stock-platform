# -*- coding: utf-8 -*-
"""东方财富功能矩阵逐项实测探针。

只调用 GET 只读接口，不触碰 capture/backfill/sync 等写库接口。
输出：raw JSON 聚合文件 + 紧凑控制台摘要。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
RAW: dict = {}


def get(path: str, timeout: float = 90.0) -> dict:
    url = BASE + path
    t0 = time.time()
    rec = {"path": path, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            rec.update(status=r.status, ms=round((time.time() - t0) * 1000), body=body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        rec.update(status=e.code, ms=round((time.time() - t0) * 1000), body=body)
    except Exception as e:  # noqa: BLE001
        rec.update(status=None, ms=round((time.time() - t0) * 1000), body="", exc=repr(e))
    return rec


def body_json(rec: dict):
    try:
        return json.loads(rec.get("body") or "")
    except Exception:  # noqa: BLE001
        return None


def record(key: str, path: str, **meta) -> dict:
    rec = get(path)
    rec["key"] = key
    RAW[key] = {**rec, **meta}
    return rec


def market_of(symbol: str) -> str:
    s = str(symbol or "").strip()
    if s.startswith(("60", "68", "51", "58", "11", "90")):
        return "SH"
    if s.startswith(("00", "30", "12", "15", "16", "18", "20", "39")):
        return "SZ"
    if s.startswith(("4", "8", "92")):
        return "BJ"
    return "?"


def walk_fields(obj, prefix="", depth=0):
    """收集返回体里所有 dict 的键路径（限深 3）。"""
    out = []
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            out.extend(walk_fields(v, p, depth + 1))
    elif isinstance(obj, list) and obj:
        out.extend(walk_fields(obj[0], prefix + "[]", depth + 1))
    return out


def rows_of(payload):
    if not isinstance(payload, dict):
        return []
    for k in ("rows", "items", "members"):
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def summarize(rec: dict, n=3) -> None:
    p = body_json(rec)
    st = rec.get("status")
    if p is None:
        print("  HTTP %s %sms BODY=%s" % (st, rec.get("ms"), (rec.get("body") or rec.get("exc") or "")[:300]))
        return
    if isinstance(p, dict) and isinstance(p.get("detail"), (str, list, dict)):
        print("  HTTP %s %sms detail=%s" % (st, rec.get("ms"), json.dumps(p["detail"], ensure_ascii=False)[:300]))
        return
    rows = rows_of(p)
    print("  HTTP %s %sms total=%s count=%s rows=%d" % (
        st, rec.get("ms"),
        p.get("total") if isinstance(p, dict) else None,
        p.get("count") if isinstance(p, dict) else None,
        len(rows)))
    if rows:
        print("  FIELDS:", json.dumps(list(rows[0].keys()), ensure_ascii=False))
        for r in rows[:n]:
            print("   SAMPLE:", json.dumps(r, ensure_ascii=False)[:600])
        markets = {}
        for r in rows:
            m = market_of(r.get("symbol"))
            markets[m] = markets.get(m, 0) + 1
        print("  MARKETS:", markets)
        # first non-empty sample per row-field
        print("  TYPEOF:", json.dumps({k: type(v).__name__ for k, v in rows[0].items()}, ensure_ascii=False))
    else:
        print("  TOPKEYS:", json.dumps(list(p.keys()), ensure_ascii=False)[:400])
        print("  BODY:", json.dumps(p, ensure_ascii=False)[:500])


# ─────────────────── main ───────────────────

def main(out_path: str) -> None:
    # 0) 目录
    record("catalog.datacenter", "/api/market/datacenter")
    record("catalog.limitup", "/api/market/limit-up")
    record("catalog.indices", "/api/market/indices")

    dc = body_json(RAW["catalog.datacenter"]) or {}
    datasets = [d["key"] for d in dc.get("datasets", [])]
    lu = body_json(RAW["catalog.limitup"]) or {}
    pools = [d["key"] for d in lu.get("pools", [])]

    print("### datasets:", datasets)
    print("### pools:", pools)

    # 1) 数据中心 12 个数据集
    for ds in datasets:
        print("\n### datacenter/%s" % ds)
        rec = record(f"dc.{ds}", f"/api/market/datacenter/{ds}?limit=20")
        summarize(rec)
        RAW[f"dc.{ds}"]["params"] = {"limit": 20}

    # 2) 龙虎榜席位（用 dc.dragon-tiger 里第一条 symbol）
    dt = body_json(RAW.get("dc.dragon-tiger", {})) or {}
    dt_rows = rows_of(dt)
    if dt_rows:
        sym = dt_rows[0].get("symbol")
        td = dt_rows[0].get("trade_date")
        print("\n### dragon-tiger seats symbol=%s trade_date=%s" % (sym, td))
        rec = record(
            "dc.dragon-tiger-seats",
            f"/api/market/datacenter/dragon-tiger/{sym}/seats?trade_date={td}",
            symbol=sym, trade_date=td)
        p = body_json(rec) or {}
        print("  HTTP %s %sms" % (rec["status"], rec["ms"]))
        for side in ("buy", "sell"):
            arr = p.get(side) or []
            print("  %s: n=%d fields=%s" % (side, len(arr), json.dumps(list(arr[0].keys()), ensure_ascii=False) if arr else None))
            if arr:
                print("   SAMPLE:", json.dumps(arr[0], ensure_ascii=False)[:400])

    # 3) 板块行情 industry/concept/region
    first_board = {}
    for kind in ("industry", "concept", "region"):
        print("\n### boards kind=%s" % kind)
        rec = record(f"boards.{kind}", f"/api/market/boards?kind={kind}&limit=20")
        summarize(rec)
        rows = rows_of(body_json(rec))
        if rows:
            first_board[kind] = rows[0].get("code") or rows[0].get("board_code")

    # 4) 成分股
    for kind, code in first_board.items():
        print("\n### boards/%s/constituents (kind=%s)" % (code, kind))
        rec = record(f"constituents.{kind}", f"/api/market/boards/{code}/constituents?limit=20")
        summarize(rec)

    # 5) 资金流
    for kind in ("industry", "concept", "region"):
        print("\n### fund-flow/boards kind=%s" % kind)
        summarize(record(f"fundflow.boards.{kind}", f"/api/market/fund-flow/boards?kind={kind}&limit=20"))
    print("\n### fund-flow/stocks")
    summarize(record("fundflow.stocks", "/api/market/fund-flow/stocks?limit=20"))
    print("\n### fund-flow/stocks/600519 (history)")
    summarize(record("fundflow.history.600519", "/api/market/fund-flow/stocks/600519?days=10"))

    # 6) 涨停池
    for pool in pools:
        print("\n### limit-up/%s" % pool)
        summarize(record(f"lu.{pool}", f"/api/market/limit-up/{pool}?limit=20"))

    # 7) 行情（指数 + 个股报价 + 分时）
    print("\n### quotes: 600519 / 000001 / 430047 / 688981")
    for sym in ("600519", "000001", "430047", "688981"):
        summarize(record(f"quote.{sym}", f"/api/quotes/{sym}"), n=1)
    print("\n### quotes batch")
    summarize(record("quote.batch", "/api/quotes/batch"), n=1)

    # 8) 失败行为
    print("\n### ERROR PATHS")
    errs = {
        "err.no-such-dataset": "/api/market/datacenter/no-such-dataset-xyz?limit=5",
        "err.bad-date": "/api/market/datacenter/dragon-tiger?date=2026-13-45&limit=5",
        "err.bad-date-fmt": "/api/market/datacenter/dragon-tiger?date=notadate&limit=5",
        "err.huge-limit": "/api/market/datacenter/dragon-tiger?limit=99999",
        "err.zero-limit": "/api/market/datacenter/dragon-tiger?limit=0",
        "err.bad-symbol": "/api/market/datacenter/margin?symbol=999999&limit=5",
        "err.malformed-symbol": "/api/market/datacenter/margin?symbol=abc&limit=5",
        "err.bad-board-kind": "/api/market/boards?kind=notakind&limit=5",
        "err.bad-pool": "/api/market/limit-up/not-a-pool?limit=5",
        "err.bad-seats-symbol": "/api/market/datacenter/dragon-tiger/12ab/seats",
        "err.bad-quote": "/api/quotes/999999",
        "err.bad-fundflow-symbol": "/api/market/fund-flow/stocks/999999",
    }
    for k, path in errs.items():
        rec = record(k, path)
        summarize(rec)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(RAW, f, ensure_ascii=False, indent=1)
    print("\nWROTE", out_path, "entries=%d" % len(RAW))


if __name__ == "__main__":
    main(sys.argv[1])
