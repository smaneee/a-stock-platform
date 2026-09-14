# -*- coding: utf-8 -*-
"""市场覆盖实测：每数据集取最大页，统计沪深京分布与更新日期。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT: dict = {}

DATE_FIELD = {
    "dragon-tiger": "trade_date", "block-trade": "trade_date", "margin": "trade_date",
    "northbound": "trade_date", "org-survey": "notice_date", "holder-number": "end_date",
    "restricted-release": "free_date", "earnings-forecast": "notice_date",
    "dividend": "notice_date", "executive-hold": "change_date", "pledge": "trade_date",
    "convertible-bond": "listing_date",
}


def get(path, timeout=180.0):
    t0 = time.time()
    try:
        req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, round((time.time() - t0) * 1000), json.loads(
                r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, round((time.time() - t0) * 1000), json.loads(
            e.read().decode("utf-8", "replace") or "{}")
    except Exception as e:  # noqa: BLE001
        return None, round((time.time() - t0) * 1000), {"exc": repr(e)}


def mkt(sym: str) -> str:
    s = str(sym or "")
    if s.startswith(("60", "68", "90", "11", "13")):
        return "SH"
    if s.startswith(("00", "30", "20", "12", "39")):
        return "SZ"
    if s.startswith(("4", "8", "92")):
        return "BJ"
    return "其他"


datasets = ["dragon-tiger", "block-trade", "margin", "northbound", "org-survey",
            "holder-number", "restricted-release", "earnings-forecast", "dividend",
            "executive-hold", "pledge", "convertible-bond"]
pools = ["limit-up", "limit-down", "broken-board", "strong", "sub-new"]

print("=== 数据中心（limit=500，单页上限） ===")
for ds in datasets:
    st, ms, p = get(f"/api/market/datacenter/{ds}?limit=500")
    rows = p.get("rows") or []
    dist: dict[str, int] = {}
    for r in rows:
        dist[mkt(r.get("symbol"))] = dist.get(mkt(r.get("symbol")), 0) + 1
    fld = DATE_FIELD.get(ds)
    vals = sorted({str(r.get(fld)) for r in rows if r.get(fld)}) if fld else []
    print(f"{ds:20s} HTTP {st} {ms:6d}ms total={p.get('total')} n={len(rows)} 市场={dist} {fld}={vals[:1]}..{vals[-1:] if vals else []}")
    OUT["dc." + ds] = {"status": st, "ms": ms, "total": p.get("total"), "n": len(rows),
                       "markets": dist, "date_field": fld,
                       "date_min": vals[0] if vals else None,
                       "date_max": vals[-1] if vals else None}

print("\n=== 涨停池（limit=200，单页上限） ===")
for pl in pools:
    st, ms, p = get(f"/api/market/limit-up/{pl}?limit=200")
    rows = p.get("items") or []
    dist: dict[str, int] = {}
    for r in rows:
        dist[mkt(r.get("symbol"))] = dist.get(mkt(r.get("symbol")), 0) + 1
    print(f"{pl:16s} HTTP {st} {ms:6d}ms trade_date={p.get('trade_date')} total={p.get('total')} n={len(rows)} 市场={dist}")
    OUT["lu." + pl] = {"status": st, "ms": ms, "trade_date": p.get("trade_date"),
                       "total": p.get("total"), "n": len(rows), "markets": dist}

print("\n=== 板块与成分股（各 kind 的最大覆盖） ===")
for kind in ("industry", "concept", "region"):
    st, ms, p = get(f"/api/market/boards?kind={kind}&limit=1000")
    items = p.get("items") or []
    print(f"boards.{kind:9s} HTTP {st} {ms:6d}ms n={len(items)} 首个={items[0]['code'] if items else None} ({items[0]['name'] if items else None})")
    OUT["boards." + kind] = {"status": st, "ms": ms, "n": len(items)}

for code in ("BK1592", "BK0976", "BK0153"):
    st, ms, p = get(f"/api/market/boards/{code}/constituents?limit=1000")
    items = p.get("items") or []
    dist: dict[str, int] = {}
    for r in items:
        dist[mkt(r.get("symbol"))] = dist.get(mkt(r.get("symbol")), 0) + 1
    print(f"constituents {code} HTTP {st} {ms:6d}ms n={len(items)} 市场={dist}")
    OUT["constituents." + code] = {"status": st, "ms": ms, "n": len(items), "markets": dist}

print("\n=== 资金流 ===")
for kind in ("industry", "concept", "region"):
    st, ms, p = get(f"/api/market/fund-flow/boards?kind={kind}&limit=1000")
    print(f"fund-flow/boards {kind:9s} HTTP {st} {ms:6d}ms n={len(p.get('items') or [])}")
    OUT["ff.boards." + kind] = {"status": st, "ms": ms, "n": len(p.get("items") or [])}
st, ms, p = get("/api/market/fund-flow/stocks?limit=1000")
items = p.get("items") or []
dist: dict[str, int] = {}
for r in items:
    dist[mkt(r.get("code"))] = dist.get(mkt(r.get("code")), 0) + 1
print(f"fund-flow/stocks HTTP {st} {ms:6d}ms n={len(items)} 市场={dist}（上限 1000）")
OUT["ff.stocks"] = {"status": st, "ms": ms, "n": len(items), "markets": dist}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("\nWROTE", sys.argv[1])
