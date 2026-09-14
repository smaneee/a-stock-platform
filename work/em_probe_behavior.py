# -*- coding: utf-8 -*-
"""功能行为（不只是字段名）实测：过滤 / 分页 / 排序 / 边界 / 口径一致性。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT: dict = {}


def get(path: str, timeout: float = 90.0) -> dict:
    t0 = time.time()
    rec = {"path": path}
    try:
        req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rec.update(status=r.status, ms=round((time.time() - t0) * 1000),
                       body=r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        rec.update(status=e.code, ms=round((time.time() - t0) * 1000),
                   body=e.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        rec.update(status=None, ms=round((time.time() - t0) * 1000), body="", exc=repr(e))
    return rec


def j(rec):
    try:
        return json.loads(rec["body"])
    except Exception:  # noqa: BLE001
        return None


def rows(rec):
    p = j(rec)
    if not isinstance(p, dict):
        return []
    for k in ("rows", "items"):
        if isinstance(p.get(k), list):
            return p[k]
    return []


def check(name, path, fn):
    rec = get(path)
    payload = j(rec)
    try:
        ok, note = fn(payload, rows(rec))
    except Exception as e:  # noqa: BLE001
        ok, note = None, f"检查异常 {e!r}"
    OUT[name] = {"path": path, "status": rec["status"], "ms": rec["ms"],
                 "ok": ok, "note": note,
                 "total": (payload or {}).get("total") if isinstance(payload, dict) else None}
    flag = {True: "PASS", False: "FAIL", None: "N/A "}[ok]
    print(f"[{flag}] {name:44s} HTTP {rec['status']} {note}")


# 1) symbol 过滤是否真的生效
def f_symbol(p, rws):
    if not rws:
        return False, "无数据"
    bad = [r["symbol"] for r in rws if r.get("symbol") != "300808"]
    return (not bad), f"n={len(rws)} 全部 symbol=300808" if not bad else f"混入 {bad[:3]}"


check("filter.dragon-tiger symbol=300808",
      "/api/market/datacenter/dragon-tiger?symbol=300808&limit=20", f_symbol)


def f_symbol_holder(p, rws):
    if not rws:
        return False, "无数据"
    bad = [r["symbol"] for r in rws if r.get("symbol") != "000725"]
    return (not bad), f"n={len(rws)} 全部 symbol=000725" if not bad else f"混入 {bad[:3]}"


check("filter.holder-number symbol=000725",
      "/api/market/datacenter/holder-number?symbol=000725&limit=20", f_symbol_holder)


# 2) date 精确过滤
def f_date(p, rws):
    if not rws:
        return False, "无数据"
    bad = {r.get("trade_date") for r in rws} - {"2026-09-11"}
    return (not bad), f"n={len(rws)} trade_date 唯一值={sorted({r.get('trade_date') for r in rws})}"


check("filter.dragon-tiger date=2026-09-11",
      "/api/market/datacenter/dragon-tiger?date=2026-09-11&limit=20", f_date)


# 3) date_from/date_to 区间
def f_range(p, rws):
    if not rws:
        return False, "无数据"
    vals = sorted({r.get("trade_date") for r in rws})
    ok = all("2026-09-08" <= v <= "2026-09-11" for v in vals)
    return ok, f"n={len(rws)} 区间内日期={vals}"


check("filter.northbound date_from/date_to",
      "/api/market/datacenter/northbound?date_from=2026-09-08&date_to=2026-09-11&limit=20", f_range)


# 4) 分页
def f_page(_, rws):
    return len(rws) == 5, f"page=2 limit=5 返回 n={len(rws)}"


check("page.dragon-tiger page=2 limit=5",
      "/api/market/datacenter/dragon-tiger?page=2&limit=5", f_page)

sym1 = [r["symbol"] for r in rows(get("/api/market/datacenter/dragon-tiger?page=1&limit=5"))]
sym2 = [r["symbol"] for r in rows(get("/api/market/datacenter/dragon-tiger?page=2&limit=5"))]
print(f"[{'PASS' if not set(sym1) & set(sym2) else 'FAIL'}] page.内容不重复 "
      f"page1={sym1} page2={sym2}")
OUT["page.distinct"] = {"ok": not set(sym1) & set(sym2), "page1": sym1, "page2": sym2}


# 5) order=asc 对手续最早解禁优先
def f_order_asc(p, rws):
    if not rws:
        return False, "无数据"
    vals = [r.get("free_date") for r in rws]
    ok = vals == sorted(vals)
    return ok, f"n={len(rws)} free_date 首/末={vals[0]}/{vals[-1]} 升序={ok}"


check("order.restricted-release asc（最早解禁优先）",
      "/api/market/datacenter/restricted-release?order=asc&limit=10", f_order_asc)


def f_order_desc(p, rws):
    if not rws:
        return False, "无数据"
    vals = [r.get("free_date") for r in rws]
    return vals == sorted(vals, reverse=True), f"默认desc 首/末={vals[0]}/{vals[-1]}（远端未来解禁优先）"


check("order.restricted-release default desc",
      "/api/market/datacenter/restricted-release?limit=10", f_order_desc)


# 6) 涨停池 trade_date 回补与上游保留窗口
def f_lu_date(p, rws):
    td = (p or {}).get("trade_date")
    return bool(rws) and td == "2026-09-11", f"trade_date={td} n={len(rws)}"


check("limit-up trade_date=2026-09-11",
      "/api/market/limit-up/limit-up?trade_date=2026-09-11&limit=20", f_lu_date)


def f_lu_old(p, rws):
    td = (p or {}).get("trade_date")
    return (not rws), f"trade_date={td} n={len(rws)}（上游保留窗口之外）"


check("limit-up trade_date=2026-06-01（超保留窗口）",
      "/api/market/limit-up/limit-up?trade_date=2026-06-01&limit=20", f_lu_old)


def f_lu_mid(p, rws):
    td = (p or {}).get("trade_date")
    return bool(rws), f"trade_date={td} n={len(rws)}"


check("limit-up trade_date=2026-08-28（保留窗口内）",
      "/api/market/limit-up/limit-up?trade_date=2026-08-28&limit=20", f_lu_mid)


# 7) 涨跌幅符号口径
def f_lu_sign(p, rws):
    if not rws:
        return False, "无数据"
    vals = [r["change_pct"] for r in rws]
    return all(v > 0 for v in vals), f"n={len(rws)} 全为正值 min={min(vals):.3f} max={max(vals):.3f}"


check("semantics.limit-up change_pct 全为正（百分数）",
      "/api/market/limit-up/limit-up?limit=20", f_lu_sign)


def f_ld_sign(p, rws):
    if not rws:
        return False, "无数据"
    vals = [r["change_pct"] for r in rws]
    return all(v < 0 for v in vals), f"n={len(rws)} 全为负值 min={min(vals):.3f} max={max(vals):.3f}"


check("semantics.limit-down change_pct 全为负（百分数）",
      "/api/market/limit-up/limit-down?limit=20", f_ld_sign)


# 8) 沪深港通：沪股通 + 深股通 == 北向合计
nb = rows(get("/api/market/datacenter/northbound?date=2026-09-11&limit=50"))
bych = {r.get("channel"): r.get("deal_amount") for r in nb}
if "沪股通" in bych and "深股通" in bych and "北向资金合计" in bych:
    s = (bych["沪股通"] or 0) + (bych["深股通"] or 0)
    t = bych["北向资金合计"] or 0
    ok = abs(s - t) / t < 1e-9 if t else False
    print(f"[{'PASS' if ok else 'FAIL'}] semantics.northbound 沪+深={s:.0f} == 合计={t:.0f}（单位:元）")
    OUT["semantics.northbound_sum"] = {"ok": ok, "hushen": s, "total": t}
else:
    print("[FAIL] semantics.northbound 缺少通道行", list(bych))
    OUT["semantics.northbound_sum"] = {"ok": False, "channels": list(bych)}
print("  northbound 样例行:", json.dumps(nb[:1], ensure_ascii=False)[:300])


# 9) 各通道字段非空率
if nb:
    nul = {k: sum(1 for r in nb if r.get(k) is None) for k in nb[0]}
    print("  northbound 空值计数:", json.dumps(nul, ensure_ascii=False))
    OUT["semantics.northbound_nulls"] = nul


# 10) 不支持过滤的数据集应 422
def f_no_date(p, rws):
    return False, f"期望 422，实际 payload={json.dumps(p, ensure_ascii=False)[:200]}"


check("reject.convertible-bond 传 date（无日期列）",
      "/api/market/datacenter/convertible-bond?date=2026-09-11&limit=5",
      lambda p, r: (True, f"422 detail={p.get('detail')!r}") if p and "detail" in p else f_no_date(p, r))


check("reject.holder-number 传不支持的 symbol？",
      "/api/market/datacenter/org-survey?symbol=600642&limit=5",
      lambda p, r: (len(r) > 0, f"n={len(r)} symbols={sorted({x.get('symbol') for x in r})}"))


# 11) 上限边界
for name, path in {
    "bound.limit-up limit=200": "/api/market/limit-up/limit-up?limit=200",
    "bound.limit-up limit=201": "/api/market/limit-up/limit-up?limit=201",
    "bound.datacenter limit=500": "/api/market/datacenter/dragon-tiger?limit=500",
    "bound.boards limit=1000": "/api/market/boards?kind=industry&limit=1000",
    "bound.boards limit=1001": "/api/market/boards?kind=industry&limit=1001",
    "bound.constituents limit=1000": "/api/market/boards/BK1592/constituents?limit=1000",
}.items():
    rec = get(path)
    print(f"[INFO] {name:38s} HTTP {rec['status']} n={len(rows(rec))} ms={rec['ms']}")
    OUT[name] = {"status": rec["status"], "n": len(rows(rec)), "ms": rec["ms"]}


# 12) 覆盖数量（不限条数时的目录规模）
for name, path, key in [
    ("coverage.boards.industry", "/api/market/boards?kind=industry&limit=1000", "items"),
    ("coverage.boards.concept", "/api/market/boards?kind=concept&limit=1000", "items"),
    ("coverage.boards.region", "/api/market/boards?kind=region&limit=1000", "items"),
    ("coverage.fundflow.stocks", "/api/market/fund-flow/stocks?limit=1000", "items"),
]:
    rec = get(path)
    p = j(rec) or {}
    items = p.get(key) or []
    syms = [i.get("code") or i.get("symbol") for i in items]
    mkt = {}
    for s in syms:
        m = ("SH" if str(s).startswith(("60", "68", "5")) else
             "SZ" if str(s).startswith(("00", "30", "1")) else
             "BJ" if str(s).startswith(("4", "8", "92")) else "?")
        mkt[m] = mkt.get(m, 0) + 1
    print(f"[INFO] {name:38s} HTTP {rec['status']} n={len(items)} 市场分布={mkt}")
    OUT[name] = {"status": rec["status"], "n": len(items), "markets": mkt, "ms": rec["ms"]}


# 13) 前端静态资源
for p in ["/", "/assets"]:
    rec = get(p)
    print(f"[INFO] frontend {p:12s} HTTP {rec['status']} len={len(rec['body'])}")
    OUT["frontend" + p] = {"status": rec["status"], "len": len(rec["body"])}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("\nWROTE", sys.argv[1])
