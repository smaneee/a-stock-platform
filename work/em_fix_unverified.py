# -*- coding: utf-8 -*-
"""把第 9 条未核实项（pledge_market_cap 单位）补入聚合 JSON，与报告保持一致。"""
from __future__ import annotations

import json
import sys

p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    d = json.load(f)

extra = {
    "项目": "pledge_market_cap 单位是否已换算为元",
    "原因": "目录表头标为「质押市值(万元)」，而 eastmoney_datacenter.py:21 声明「全部换算成元」；实测 300010 该值 25164.0 相对其市值明显偏小，疑似仍为万元。未取得上游列定义，故不判定",
    "实测": "curl \"http://127.0.0.1:8000/api/market/datacenter/pledge?limit=200\" → 300010 pledge_market_cap=25164.0，pledge_shares=4660.0（万股）",
}
if all(x["项目"] != extra["项目"] for x in d["unverified"]):
    d["unverified"].append(extra)
d["unit_caveats"] = {
    "pledge_market_cap": "表头「万元」与「全部换算成元」的声明冲突，列入未核实项（unverified 第 9 条）",
    "其余金额字段": "均为元，已由北向「沪股通+深股通=北向合计」误差 0 与龙虎榜「净额=买入-卖出」完全一致交叉验证",
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
print("unverified 条数 =", len(d["unverified"]))
print("issues 条数 =", len(d["issues"]))
