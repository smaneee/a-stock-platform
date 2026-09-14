# -*- coding: utf-8 -*-
"""分页稳定性与字段空值率实测。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = {}


def rows(path):
    t0 = time.time()
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        p = json.loads(r.read().decode("utf-8", "replace"))
    return p, round((time.time() - t0) * 1000)


# 1) margin：默认按单列 DATE 降序，同日 680 万条，检查分页是否稳定
p1, ms1 = rows("/api/market/datacenter/margin?limit=500&page=1")
p2, ms2 = rows("/api/market/datacenter/margin?limit=500&page=2")
s1 = [r["symbol"] for r in p1["rows"]]
s2 = [r["symbol"] for r in p2["rows"]]
overlap = len(set(s1) & set(s2))
print(f"margin page1 n={len(s1)} page2 n={len(s2)} 交集={overlap}")
print(f"  page1 市场: SH={sum(1 for x in s1 if x.startswith('60'))} "
      f"SZ={sum(1 for x in s1 if x.startswith(('00','30')))} "
      f"其他={sum(1 for x in s1 if not x.startswith(('60','00','30')))}")
print(f"  page2 市场: SH={sum(1 for x in s2 if x.startswith('60'))} "
      f"SZ={sum(1 for x in s2 if x.startswith(('00','30')))} "
      f"其他={sum(1 for x in s2 if not x.startswith(('60','00','30')))}")
# 重复请求同一页，看是否稳定
p1b, _ = rows("/api/market/datacenter/margin?limit=500&page=1")
s1b = [r["symbol"] for r in p1b["rows"]]
same = s1 == s1b
print(f"  page1 重复请求结果一致={same} 差异={len(set(s1) ^ set(s1b))}")
OUT["margin_paging"] = {"page1_n": len(s1), "page2_n": len(s2), "overlap": overlap,
                        "repeat_stable": same, "ms1": ms1, "ms2": ms2,
                        "page1_markets": {"SH": sum(1 for x in s1 if x.startswith('60')),
                                          "SZ": sum(1 for x in s1 if x.startswith(('00','30')))},
                        "page2_markets": {"SH": sum(1 for x in s2 if x.startswith('60')),
                                          "SZ": sum(1 for x in s2 if x.startswith(('00','30')))}}

# 2) 龙虎榜：上榜后涨跌幅字段空值率
p, _ = rows("/api/market/datacenter/dragon-tiger?limit=500")
rws = p["rows"]
nulls = {k: sum(1 for r in rws if r.get(k) is None) for k in rws[0]}
print(f"\ndragon-tiger n={len(rws)} 空值计数={json.dumps(nulls, ensure_ascii=False)}")
OUT["dragon_tiger_nulls"] = {"n": len(rws), "nulls": nulls}

# 3) 龙虎榜 席位返回的买卖金额量级 vs 主表（口径交叉核对）
sym = rws[0]["symbol"]
td = rws[0]["trade_date"]
seat = None
with urllib.request.urlopen(
        f"{BASE}/api/market/datacenter/dragon-tiger/{sym}/seats?trade_date={td}", timeout=90) as r:
    seat = json.loads(r.read().decode("utf-8", "replace"))
buy_sum = sum(s["buy_amount"] for s in seat["buy"])
main = [r for r in rws if r["symbol"] == sym][0]
print(f"\n席位交叉核对 {sym} {td}: 买席位买入额合计={buy_sum:.0f} 主表 billboard_buy_amount={main['billboard_buy_amount']:.0f}")
print(f"  比值={buy_sum / main['billboard_buy_amount']:.4f}（主表应为全部席位合计，>= 前5席位合计）")
OUT["seats_crosscheck"] = {"symbol": sym, "trade_date": td, "seat_buy_sum": buy_sum,
                          "billboard_buy": main["billboard_buy_amount"],
                          "ratio": buy_sum / main["billboard_buy_amount"],
                          "seat_n": len(seat["buy"])}

# 4) 各数据集"声明字段全空"比例（抽 limit=200）
print("\n=== 各数据集字段空值率（limit=200 抽样） ===")
for ds in ["dragon-tiger", "block-trade", "margin", "northbound", "org-survey",
           "holder-number", "restricted-release", "earnings-forecast", "dividend",
           "executive-hold", "pledge", "convertible-bond"]:
    p, _ = rows(f"/api/market/datacenter/{ds}?limit=200")
    rr = p["rows"]
    if not rr:
        print(f"{ds:20s} 无数据")
        continue
    allnull = [k for k in rr[0] if all(r.get(k) is None for r in rr)]
    partial = {k: sum(1 for r in rr if r.get(k) is None) for k in rr[0]
               if 0 < sum(1 for r in rr if r.get(k) is None) < len(rr)}
    print(f"{ds:20s} n={len(rr):3d} 恒空={allnull} 部分空={partial}")
    OUT["nulls." + ds] = {"n": len(rr), "always_null": allnull, "partial_null": partial}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("\nWROTE", sys.argv[1])
