"""回补「历史时点股票池快照」，消除幸存者偏差。

为什么必须做
------------
增量日线回补默认以「最新快照」的成员为准，那只包含**今天还活着**的 5550 只。
用它回测 2022 年会漏掉三件事：
1. 当年存在、后来退市的标的（幸存者偏差，最致命）；
2. 当年还没上市的新股（未来函数）；
3. 当年正常交易的 ST / 停牌状态差异。

BaoStock 的 ``query_all_stock(day)`` 是**真正的时点**接口：给定历史某一天，返回
当天在场的全部证券（含后来退市的），因此可以补出 point-in-time 快照。

用法::

    # 默认补 5 个风格锚点（2021-06 / 2022-06 / 2023-06 / 2024-06 / 2025-06）
    python scripts/backfill_universe.py

    # 指定日期
    python scripts/backfill_universe.py --days 2022-06-30,2023-12-29

    # 已存在同交易日快照时强制覆盖
    python scripts/backfill_universe.py --force

行为约束
--------
- 单日 ``query_all_stock`` + ``query_stock_basic`` 实测 12~90s，超时给 400s；
- 每个锚点一次独立事务，失败只影响该锚点，报告里列明；
- **不修改**最新快照，选股/看盘的「当前池」不受影响；
- ``query_stock_basic`` 的 status/outDate 是**当前状态**（BaoStock 不提供历史
  status），因此快照的「当天是否在场」以 ``query_all_stock`` 为准，
  该字段只作参考 —— 报告里会如实标注。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# SQLAlchemy 在 create_engine 时就把 sqlite:///./a_stock.db 解析成相对路径，
# 必须在 import app.* 之前切到 backend（否则会连到项目根目录的空库）。
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select, text  # noqa: E402

from app.database.models import UniverseMember, UniverseSnapshot  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.market_rules.calendar import TradingCalendar  # noqa: E402
from app.time_utils import utc_now  # noqa: E402
from app.universe.providers import BaoStockUniverseProvider, ProviderError  # noqa: E402
from app.universe.snapshot_service import UniverseSnapshotService  # noqa: E402

DEFAULT_DAYS = "2021-06-30,2022-06-30,2023-06-30,2024-06-28,2025-06-30"
FETCH_TIMEOUT_SECONDS = 600.0
# BaoStock 免费接口会随机「接收数据异常 / timed out」，单次失败不代表该时点取不到，
# 因此失败要重试；重试间隔给足，避免立刻撞上同一个抖动窗口。
MAX_RETRIES = 2
# 一次同步要 upsert 5000+ 行 securities，是长事务；后端 / 回补进程若同时持写锁，
# 默认 5s 等待必然失败。只放宽本脚本连接的等待上限，不改生产配置。
BUSY_TIMEOUT_MS = 120_000


def existing_snapshot(day: date) -> tuple[int, int] | None:
    with SessionLocal() as db:
        normalized = TradingCalendar(db).last_trading_day_on_or_before(day)
        snap = db.scalar(
            select(UniverseSnapshot).where(UniverseSnapshot.trading_day == normalized)
        )
        if snap is None:
            return None
        members = db.scalar(
            select(func.count())
            .select_from(UniverseMember)
            .where(UniverseMember.snapshot_id == snap.id)
        )
        return snap.id, int(members or 0)


def snapshot_summary(snapshot_id: int) -> dict[str, int]:
    with SessionLocal() as db:
        rows = db.execute(
            select(UniverseMember.trading_status, func.count())
            .where(UniverseMember.snapshot_id == snapshot_id)
            .group_by(UniverseMember.trading_status)
        ).all()
        included = db.scalar(
            select(func.count())
            .select_from(UniverseMember)
            .where(UniverseMember.snapshot_id == snapshot_id)
            .where(UniverseMember.is_included.is_(True))
        )
    summary = {str(status): int(count) for status, count in rows}
    summary["included"] = int(included or 0)
    return summary


async def run(args: argparse.Namespace) -> int:
    days = [date.fromisoformat(token.strip()) for token in args.days.split(",") if token.strip()]
    report: dict[str, object] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "anchors": [],
    }
    anchors: list[dict[str, object]] = report["anchors"]  # type: ignore[assignment]

    for day in days:
        print(f"\n=== 锚点 {day} ===", flush=True)
        if not args.force:
            hit = existing_snapshot(day)
            if hit is not None:
                snap_id, members = hit
                print(f"  已存在快照 #{snap_id}（{members} 条成员），跳过（--force 可覆盖）", flush=True)
                anchors.append(
                    {
                        "requested_day": day.isoformat(),
                        "status": "skipped_exists",
                        "snapshot_id": snap_id,
                        "members": members,
                    }
                )
                continue

        provider = BaoStockUniverseProvider(
            timeout_seconds=FETCH_TIMEOUT_SECONDS, as_of_date=day
        )
        started = time.perf_counter()
        records = None
        last_error = ""
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                records = await provider.fetch_all()
                break
            except ProviderError as exc:
                last_error = str(exc)[:300]
                print(f"  第 {attempt} 次拉取失败：{exc}", flush=True)
                if attempt <= MAX_RETRIES:
                    await asyncio.sleep(5.0 * attempt)
        if records is None:
            elapsed = time.perf_counter() - started
            print(f"  失败（{elapsed:.0f}s）：{last_error}", flush=True)
            anchors.append(
                {
                    "requested_day": day.isoformat(),
                    "status": "failed",
                    "error": last_error,
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
            continue

        # effective_day 由 provider 从交易日历归一，周末/节假日不会落成快照日
        effective_day = next(
            (r.as_of_date for r in records if r.as_of_date is not None), day
        )
        with SessionLocal() as db:
            db.execute(text(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"))
            snap = UniverseSnapshotService(db).get_or_create_snapshot_from_records(
                records=records,
                trading_day=effective_day,
                source_provider=provider.source_id,
                source_synced_at=utc_now(),
                as_of_date=effective_day,
                force_overwrite=True,
            )
            db.commit()
            snapshot_id = snap.id
            snapshot_day = snap.trading_day.isoformat()
            total_count = snap.total_count
            included_count = snap.included_count
        elapsed = time.perf_counter() - started
        summary = snapshot_summary(snapshot_id)
        print(
            f"  快照 #{snapshot_id} 交易日 {snapshot_day}；当日清单 {total_count} 条，"
            f"入选 {included_count}；{elapsed:.0f}s",
            flush=True,
        )
        print(f"  成员分布：{summary}", flush=True)
        anchors.append(
            {
                "requested_day": day.isoformat(),
                "status": "ok",
                "snapshot_id": snapshot_id,
                "snapshot_trading_day": snapshot_day,
                "total_fetched": total_count,
                "included_count": included_count,
                "members": summary,
                "elapsed_seconds": round(elapsed, 1),
            }
        )

    out = Path(args.out) if args.out else (
        ROOT / "outputs" / f"backfill_universe_{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for item in anchors if item["status"] == "ok")
    failed = sum(1 for item in anchors if item["status"] == "failed")
    skipped = sum(1 for item in anchors if item["status"] == "skipped_exists")
    print(f"\n完成：成功 {ok}，跳过 {skipped}，失败 {failed}\n报告：{out}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="回补历史时点股票池快照（BaoStock）")
    parser.add_argument("--days", default=DEFAULT_DAYS, help=f"锚点日期，逗号分隔，默认 {DEFAULT_DAYS}")
    parser.add_argument("--force", action="store_true", help="已存在同交易日快照时强制覆盖")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
