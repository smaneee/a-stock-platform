import re
from collections import Counter
p = r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform\outputs\handoff\eastmoney-feature-matrix.md"
lines = open(p, encoding="utf-8").read().split("\n")
# 主表区间：从表头行到第 9 节之前
start = next(i for i, ln in enumerate(lines) if ln.startswith("| # | 数据集 |"))
end = next(i for i, ln in enumerate(lines) if ln.startswith("## 3."))
seg = lines[start:end]
rows = [ln for ln in seg if re.match(r"^\| \d+ \|", ln)]
print("主表数据行数:", len(rows))
bad = [(i, len(re.split(r"(?<!\\)\|", ln)) - 2) for i, ln in enumerate(rows) if len(re.split(r"(?<!\\)\|", ln)) - 2 != 11]
print("列数不为 11 的行:", bad)
c = Counter(re.split(r"(?<!\\)\|", ln)[1:-1][9].strip() for ln in rows)
for k, v in c.items():
    print(f"  {k}: {v}")
print("合计:", sum(c.values()))
