# -*- coding: utf-8 -*-
"""对比：目录声明的字段 vs 实测返回字段 vs 前端消费点。"""
from __future__ import annotations

import json
import re
import sys

proj = sys.argv[1]
with open(proj + r"\work\catalog.json", encoding="utf-8") as f:
    cat = json.load(f)
with open(proj + r"\work\probe_all.json", encoding="utf-8") as f:
    raw = json.load(f)

dc_cat = {d["key"]: [f0["key"] for f0 in d["fields"]] for d in json.loads(cat["/api/market/datacenter"]["body"])["datasets"]}
lu_cat = {d["key"]: [f0["key"] for f0 in d["fields"]] for d in json.loads(cat["/api/market/limit-up"]["body"])["pools"]}


def actual_fields(key):
    rec = raw.get(key)
    if not rec:
        return None
    try:
        p = json.loads(rec["body"])
    except Exception:
        return None
    rows = p.get("rows") or p.get("items") or []
    return list(rows[0].keys()) if rows else []


print("=== 数据中心：声明 vs 实测 ===")
for k, dec in dc_cat.items():
    act = actual_fields("dc." + k)
    if act is None:
        print(f"{k:22s} NO-DATA")
        continue
    missing = [x for x in dec if x not in act]
    extra = [x for x in act if x not in dec]
    print(f"{k:22s} declared={len(dec):2d} actual={len(act):2d} missing={missing} extra={extra}")

print("\n=== 涨停池：声明 vs 实测 ===")
for k, dec in lu_cat.items():
    act = actual_fields("lu." + k)
    if act is None:
        print(f"{k:22s} NO-DATA")
        continue
    missing = [x for x in dec if x not in act]
    extra = [x for x in act if x not in dec]
    print(f"{k:22s} declared={len(dec):2d} actual={len(act):2d} missing={missing} extra={extra}")

print("\n=== 时效/覆盖 (实测) ===")
date_keys = {
    "dc.dragon-tiger": "trade_date", "dc.block-trade": "trade_date", "dc.margin": "trade_date",
    "dc.northbound": "trade_date", "dc.org-survey": "notice_date", "dc.holder-number": "end_date",
    "dc.restricted-release": "free_date", "dc.earnings-forecast": "notice_date",
    "dc.dividend": "notice_date", "dc.executive-hold": "change_date", "dc.pledge": "trade_date",
}
for k, fld in date_keys.items():
    rec = raw.get(k) or {}
    try:
        rows = json.loads(rec["body"]).get("rows") or []
    except Exception:
        rows = []
    vals = [r.get(fld) for r in rows if r.get(fld)]
    print(f"{k:26s} n={len(rows):3d} {fld}: min={min(vals) if vals else None} max={max(vals) if vals else None} distinct={len(set(vals))}")

print("\n=== 各接口耗时 (ms) ===")
for k, rec in raw.items():
    if rec.get("status") is not None:
        print(f"{k:34s} HTTP {rec['status']:3d} {rec['ms']:6d}ms")
