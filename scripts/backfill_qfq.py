"""全市场前复权日线回填（通达信除权除息 + 本地未复权日线）。

用法::

    python scripts/backfill_qfq.py                  # 全市场（约 5500 只）
    python scripts/backfill_qfq.py 600519 000001    # 只回填指定标的
    python scripts/backfill_qfq.py --workers 6      # 并发连接数（默认 4）

行为约束：

- 只写 ``historical_bars`` 里 ``adjust='qfq'`` 的行，按标的「先删后插」整体重写，
  可重复执行；不改动 ``adjust='none'`` 的任何数据，不动其它表；
- 单只标的失败（拿不到除权除息 / 写库异常）只记入报告并跳过，不会写入
  「看起来像前复权、实际是不复权」的数据。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# 必须在 import app.* 之前切到 backend，否则 SQLite 相对路径 sqlite:///./a_stock.db
# 会在当前工作目录另建一个空库。
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

from app.history.qfq_backfill import QfqBackfillService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场前复权日线回填")
    parser.add_argument("symbols", nargs="*", help="只回填这些标的（缺省为全市场）")
    parser.add_argument("--workers", type=int, default=4, help="并发连接数，默认 4")
    parser.add_argument("--batch-size", type=int, default=120, help="每批标的数，默认 120")
    args = parser.parse_args()

    service = QfqBackfillService(workers=args.workers, batch_size=args.batch_size)

    def on_progress(done: int, total: int) -> None:
        step = max(1, total // 20)
        if done % step == 0 or done == total:
            print(f"进度 {done}/{total}", flush=True)

    report = service.run(
        symbols=args.symbols or None,
        progress=on_progress,
    )
    summary = report.as_dict()
    print("=" * 60)
    print(f"用时           : {summary['elapsed_seconds']}s")
    print(f"标的           : 共 {summary['symbols_total']}，"
          f"成功 {summary['symbols_ok']}，空数据 {summary['symbols_empty']}，"
          f"失败 {summary['symbols_failed']}")
    print(f"写入行数       : {summary['rows_written']}")
    print(f"应用事件数     : {summary['events_applied']}")
    print(f"未解事件标的数 : {summary['unresolved_count']}")
    if summary["failures"]:
        print("失败样例       :")
        for symbol, error in list(summary["failures"].items())[:10]:
            print(f"  {symbol}: {error}")
    return 0 if summary["symbols_failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())