# -*- coding: utf-8 -*-
"""补测：northbound 常空字段、前端静态资源、交易日/会话。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
out = {}


def get(path):
    try:
        req = urllib.request.Request(BASE + path, headers={"Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


st, body = get("/api/market/datacenter/northbound?date=2026-09-11&limit=50")
rows = json.loads(body)["rows"]
print("northbound 2026-09-11 行数 =", len(rows))
always_null = []
for k in rows[0]:
    n_null = sum(1 for r in rows if r.get(k) is None)
    if n_null == len(rows):
        always_null.append(k)
print("恒为 null 的字段:", always_null)
print("通道:", [r["channel"] for r in rows])
for r in rows:
    print("  ", r["channel"], "deal=", r["deal_amount"], "buy=", r["buy_amount"],
          "sell=", r["sell_amount"], "net=", r["net_deal_amount"],
          "fund_inflow=", r["fund_inflow"], "quota=", r["quota_balance"])
out["northbound"] = {"rows": len(rows), "always_null": always_null,
                     "channels": [r["channel"] for r in rows]}

# 前端入口与主 bundle
st, html = get("/")
print("\n/ HTTP", st, "len", len(html))
assets = []
for token in html.replace('"', " ").replace("'", " ").split():
    if token.startswith("/assets/") or token.endswith(".js") or token.endswith(".css"):
        assets.append(token.strip("><"))
print("index.html 引用资源:", assets)
out["index_refs"] = assets
for a in assets[:4]:
    p = a if a.startswith("/") else "/" + a
    st2, b2 = get(p)
    print(f"  {p:44s} HTTP {st2} len={len(b2)}")
    out["asset:" + p] = {"status": st2, "len": len(b2)}

# 交易时段
st, b = get("/api/market/session")
print("\n/market/session HTTP", st, b[:300])
out["session"] = b

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("WROTE", sys.argv[1])
