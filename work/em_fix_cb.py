# -*- coding: utf-8 -*-
"""修正可转债的市场口径：债券代码 vs 正股代码。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
with urllib.request.urlopen(
        BASE + "/api/market/datacenter/convertible-bond?limit=500", timeout=180) as r:
    rows = json.loads(r.read().decode("utf-8", "replace"))["rows"]


def bond_mkt(s):
    s = str(s)
    if s.startswith(("110", "111", "113", "118")):
        return "SH转债"
    if s.startswith(("123", "127", "128", "12")):
        return "SZ转债"
    if s.startswith("4"):
        return "退债/三板(4xxxxx)"
    return "其他"


def stock_mkt(s):
    s = str(s or "")
    if s.startswith(("60", "68")):
        return "SH正股"
    if s.startswith(("00", "30")):
        return "SZ正股"
    if s.startswith(("4", "8", "92")):
        return "BJ正股"
    return "其他/无正股"


bd, sd = {}, {}
for r in rows:
    bd[bond_mkt(r["symbol"])] = bd.get(bond_mkt(r["symbol"]), 0) + 1
    sd[stock_mkt(r.get("stock_symbol"))] = sd.get(stock_mkt(r.get("stock_symbol")), 0) + 1
print("n =", len(rows))
print("债券代码口径:", bd)
print("对应正股口径:", sd)
print("样例:", json.dumps(rows[:2], ensure_ascii=False)[:300])

# 回填到聚合 JSON
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["datasets"]["convertible-bond"]["markets_limit500"] = {
    "口径说明": "该数据集的两类代码：symbol=转债代码，stock_symbol=正股代码（原自动分类把 4xxxxx 误判为北交所，此处已修正）",
    "转债代码": bd,
    "对应正股": sd,
    "n": len(rows),
}
d["datasets"]["convertible-bond"]["_market_note"] = "债券代码非股票代码，不能按 60/00/30/8 前缀判市场"
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
print("已修正", p)
