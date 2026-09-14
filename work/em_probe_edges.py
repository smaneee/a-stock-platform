# -*- coding: utf-8 -*-
"""收尾：page2/page3 一致性、未知板块、边界页码、错误体是否泄漏堆栈。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = {}


def raw(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


def syms(path):
    st, b = raw(path)
    try:
        return [r["symbol"] for r in json.loads(b).get("rows", [])]
    except Exception:  # noqa: BLE001
        return []


s2 = syms("/api/market/datacenter/margin?limit=500&page=2")
s3 = syms("/api/market/datacenter/margin?limit=500&page=3")
print(f"margin page2 vs page3: n={len(s2)}/{len(s3)} 交集={len(set(s2) & set(s3))} 完全相同={s2 == s3}")
OUT["page2_vs_page3"] = {"n2": len(s2), "n3": len(s3), "overlap": len(set(s2) & set(s3)),
                         "identical": s2 == s3}

cases = {
    "未知板块代码": "/api/market/boards/BK9999/constituents?limit=5",
    "空板块代码": "/api/market/boards/XX/constituents?limit=5",
    "page=0": "/api/market/datacenter/dragon-tiger?page=0&limit=5",
    "page=201": "/api/market/datacenter/dragon-tiger?page=201&limit=5",
    "page=99999": "/api/market/datacenter/dragon-tiger?page=99999&limit=5",
    "不存在的股票代码(龙虎榜)": "/api/market/datacenter/dragon-tiger?symbol=999999&limit=5",
    "未来日期": "/api/market/datacenter/dragon-tiger?date=2099-01-01&limit=5",
    "非法日期 2026-13-45": "/api/market/datacenter/dragon-tiger?date=2026-13-45&limit=5",
    "非法月份 2026-02-30": "/api/market/datacenter/dragon-tiger?date=2026-02-30&limit=5",
    "不存在的池 trade_date 未来": "/api/market/limit-up/limit-up?trade_date=2099-01-01&limit=5",
    "不存在的 pool 大小写": "/api/market/limit-up/LIMIT-UP?limit=5",
    "数据集名大小写": "/api/market/datacenter/DRAGON-TIGER?limit=5",
    "板块 kind 大写": "/api/market/boards?kind=INDUSTRY&limit=5",
    "seats 不存在的日期": "/api/market/datacenter/dragon-tiger/300808/seats?trade_date=2099-01-01",
}
for name, path in cases.items():
    st, b = raw(path)
    leak = ("Traceback" in b) or ("File \"" in b) or ("raise " in b)
    short = b if len(b) < 240 else b[:240] + "…"
    print(f"\n[{name}] HTTP {st} 堆栈泄漏={leak}\n  {short}")
    OUT[name] = {"path": path, "status": st, "stack_leak": leak, "body": short}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("\nWROTE", sys.argv[1])
