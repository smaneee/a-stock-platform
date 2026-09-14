# -*- coding: utf-8 -*-
"""融资融券分页缺陷定位 + 沪深京可达性。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = {}


def rows(path):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def mkt(s):
    return ("SH" if s.startswith("60") else "SZ" if s.startswith(("00", "30"))
            else "其他")


p1 = rows("/api/market/datacenter/margin?limit=500&page=1")["rows"]
print("margin page1 首5:", [r["symbol"] for r in p1[:5]], "末2:", [r["symbol"] for r in p1[-2:]])
OUT["page1_head"] = [r["symbol"] for r in p1[:5]]
OUT["page1_tail"] = [r["symbol"] for r in p1[-2:]]

for pg in (2, 3, 10, 50):
    pr = rows(f"/api/market/datacenter/margin?limit=500&page={pg}")["rows"]
    sym = [r["symbol"] for r in pr]
    ov = len(set(sym) & {r["symbol"] for r in p1})
    print(f"margin page{pg:<3d} 首3={sym[:3]} 交集(page1)={ov}/500 市场={ {m: sum(1 for x in sym if mkt(x)==m) for m in ('SH','SZ','其他')} }")
    OUT[f"page{pg}"] = {"head": sym[:3], "overlap_with_page1": ov,
                        "markets": {m: sum(1 for x in sym if mkt(x) == m) for m in ("SH", "SZ", "其他")}}

# 沪深京可达性：按 symbol 精确查
for s in ("000001", "600519", "920392", "300750", "430047"):
    p = rows(f"/api/market/datacenter/margin?symbol={s}&limit=5")
    rr = p["rows"]
    print(f"margin symbol={s} n={len(rr)}",
          (rr[0]["trade_date"], rr[0]["market"], rr[0]["financing_balance"]) if rr else "—")
    OUT["symbol_" + s] = {"n": len(rr),
                          "row": rr[0] if rr else None}

# 同日多行（同一股票多个交易日）
p = rows("/api/market/datacenter/margin?symbol=600519&limit=50")
print("margin 600519 记录数:", len(p["rows"]), "total:", p["total"],
      "日期:", sorted({r["trade_date"] for r in p["rows"]}))
OUT["margin_600519"] = {"n": len(p["rows"]), "total": p["total"],
                        "dates": sorted({r["trade_date"] for r in p["rows"]})}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("WROTE", sys.argv[1])
