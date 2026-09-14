import re
p = r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform\outputs\handoff\eastmoney-feature-matrix.md"
lines = open(p, encoding="utf-8").read().split("\n")
print("总行数:", len(lines))
bad = []
tablerows = 0
for i, ln in enumerate(lines, 1):
    if not ln.startswith("|"):
        continue
    tablerows += 1
    cells = re.split(r"(?<!\\)\|", ln)
    # 首尾空串
    n = len(cells) - 2
    if n not in (11,):
        bad.append((i, n, ln[:70]))
print("表格行数:", tablerows)
print("列数异常行:", len(bad))
for b in bad[:12]:
    print("  line", b[0], "cells", b[1], "|", b[2])
# 统计主表数据行
main = [ln for ln in lines if re.match(r"^\| \d+ \|", ln)]
print("主表编号行数:", len(main))
print("主表编号:", [re.match(r"^\| (\d+) \|", ln).group(1) for ln in main])
# 状态计数
for st in ("**通过**", "**部分通过**", "**未通过**", "**未核实**"):
    print(st, "出现次数:", sum(ln.count(st) for ln in main))
