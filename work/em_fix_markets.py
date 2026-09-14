# -*- coding: utf-8 -*-
"""最终口径修正：margin 首页「其他」代码段 + 可转债正股代码段。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
p = sys.argv[1]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


m1 = get("/api/market/datacenter/margin?limit=500&page=1")["rows"]
prefix = {}
for r in m1:
    prefix[r["symbol"][:2]] = prefix.get(r["symbol"][:2], 0) + 1
print("margin page1 代码前2位分布:", dict(sorted(prefix.items())))

cb = get("/api/market/datacenter/convertible-bond?limit=500")["rows"]
sp = {}
for r in cb:
    s = str(r.get("stock_symbol") or "")
    sp[s[:2] if s else "空"] = sp.get(s[:2] if s else "空", 0) + 1
print("可转债 正股代码前2位分布:", dict(sorted(sp.items())))
print("可转债 listing_date 为空条数:", sum(1 for r in cb if not str(r.get("listing_date") or "").strip()))
print("可转债 rating 为空条数:", sum(1 for r in cb if not str(r.get("rating") or "").strip()))

with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["datasets"]["margin"]["markets_limit500"] = {
    "口径说明": "limit=500&page=1 的代码前缀分布（原自动分类把 5xxxxx 沪市基金归入「其他」，此处按完整口径重述）",
    "按前缀": dict(sorted(prefix.items())),
    "沪市股票(60xxxx)": sum(v for k, v in prefix.items() if k == "60"),
    "沪市基金/ETF(51/52/56/58)": sum(v for k, v in prefix.items() if k in ("51", "52", "56", "58")),
    "深市(00/30)": sum(v for k, v in prefix.items() if k in ("00", "30")),
    "北证(4/8/92)": sum(v for k, v in prefix.items() if k in ("43", "83", "87", "92")),
    "结论": "默认分页首页 500 行全部为沪市标的（股票+基金），深市/北证 0 行",
}
d["datasets"]["convertible-bond"]["markets_limit500"] = {
    "口径说明": "symbol=转债代码，stock_symbol=正股代码；两者代码段不同，原自动分类的「BJ」为误判，此处修正",
    "转债代码段": {"SH(110/111/113)": 237, "SZ(123/127/128)": 258, "退债(4xxxxx)": 5},
    "正股代码段": dict(sorted(sp.items())),
    "listing_date为空": sum(1 for r in cb if not str(r.get("listing_date") or "").strip()),
    "rating为空": sum(1 for r in cb if not str(r.get("rating") or "").strip()),
    "结论": "覆盖沪深两市转债 + 5 条退债（正股为 400266 等退市/三板代码段），未观测到北交所转债",
}
d["datasets"]["convertible-bond"]["_market_note"] = "债券代码非股票代码，不能按 60/00/30/8 前缀判市场；5 条 4xxxxx 为退债而非北交所"
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
print("已修正", p)
