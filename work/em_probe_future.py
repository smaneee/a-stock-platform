# -*- coding: utf-8 -*-
"""核实：请求未来 trade_date 时涨停池是否用最新快照冒名顶替。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"


def pool(td=None, limit=200):
    p = f"/api/market/limit-up/limit-up?limit={limit}"
    if td:
        p += f"&trade_date={td}"
    with urllib.request.urlopen(BASE + p, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


res = {}
for td in (None, "2026-09-11", "2099-01-01", "2026-12-31", "2026-06-01"):
    d = pool(td)
    syms = [x["symbol"] for x in d["items"]]
    res[str(td)] = {"trade_date_echo": d["trade_date"], "total": d["total"],
                    "n": len(syms), "syms": syms}
    print(f"请求 trade_date={str(td):12s} → 响应 trade_date={d['trade_date']:12s} total={d['total']:4d} n={len(syms)} 首3={syms[:3]}")

base = set(res["2026-09-11"]["syms"])
for k in ("2099-01-01", "2026-12-31", "2026-06-01"):
    s = set(res[k]["syms"])
    print(f"\n{k}: 响应回显 trade_date={res[k]['trade_date_echo']}，与 2026-09-11 快照交集={len(s & base)}/{len(s)}"
          f" 完全相同={s == base}")
    res[k]["same_as_20260911"] = (s == base)

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("\nWROTE", sys.argv[1])
