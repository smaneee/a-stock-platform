import re
p = r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform\outputs\handoff\eastmoney-feature-matrix.md"
lines = open(p, encoding="utf-8").read().split("\n")
main = [(i, ln) for i, ln in enumerate(lines, 1) if re.match(r"^\| \d+ \|", ln) and len(ln) > 200]
print("主表数据行数:", len(main))
ok = True
for i, ln in main:
    cells = re.split(r"(?<!\\)\|", ln)
    n = len(cells) - 2
    if n != 11:
        ok = False
        print("  列数异常 line", i, "cells", n)
print("主表全部 11 列:", ok)
# 状态列（第 10 个单元格，索引 9）
from collections import Counter
c = Counter()
for i, ln in main:
    cells = re.split(r"(?<!\\)\|", ln)[1:-1]
    c[cells[9].strip()] += 1
for k, v in c.items():
    print(f"  {k}: {v}")
