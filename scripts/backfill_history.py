"""把日线历史回补到指定区间（研究/严谨回测用的数据准备工具）。

背景
----
``/api/history/ingest`` 的任务窗口最长 2000 天，且以「当前股票池快照」为准；
要在本地攒出跨越多年、可用于样本外回测的日线，需要：先补齐交易日历，再按标的
增量抓取（``HistoricalDataService`` 已经是增量的：只补缺口，已覆盖的标的不会重复下载）。

用法::

    # 用最新股票池快照，回补到 2021-03-25 起（当前全市场约 6~10 分钟）
    python scripts/backfill_history.py --start 2021-03-25

    # 只跑前 30 只（先量一下吞吐，再决定并发）
    python scripts/backfill_history.py --start 2021-03-25 --limit 30 --concurrency 8

    # 用某个历史时点快照的成员（含已退市标的）回补
    python scripts/backfill_history.py --snapshot-day 2022-06-30 --start 2017-07-01

    # 顺带把交易日历往前铺到区间起点（必须做，否则增量逻辑会认为「历史已完整」）
    python scripts/backfill_history.py --start 2021-03-25 --extend-calendar

行为约束：

- 只写 ``historical_bars``；不做任何合成/插值，抓不到就是抓不到，失败标的会列在报告里；
- 每只标的用独立短事务，跑完即落库，中途中断可重复执行（下一次只补缺口）；
- 并发只影响网络请求，不改变任何业务口径。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
# SQLAlchemy 在 create_engine 时就把 sqlite:///./a_stock.db 解析成相对路径，
# 必须在 import app.* 之前切到 backend（否则会连到项目根目录的空库）。
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.database.models import UniverseMember, UniverseSnapshot  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.history.service import HistoricalDataService  # noqa: E402
from app.main import _build_providers  # noqa: E402  （复用生产数据源工厂，避免两份配置漂移）
from app.market_data.provider_manager import ProviderManager  # noqa: E402
from app.market_rules.calendar import sync_trading_calendar  # noqa: E402

DEFAULT_START = "2021-03-25"


def resolve_symbols(snapshot_day: date | None, limit: int | None) -> tuple[list[str], str]:
    """取某个股票池快照里「入选且正常交易」的标的；缺省用最近一次快照。"""
    with SessionLocal() as db:
        if snapshot_day is None:
            snapshot = db.scalar(
                select(UniverseSnapshot).order_by(UniverseSnapshot.trading_day.desc()).limit(1)
            )
        else:
            snapshot = db.scalar(
                select(UniverseSnapshot).where(UniverseSnapshot.trading_day == snapshot_day)
            )
        if snapshot is None:
            raise SystemExit(f"没有找到股票池快照（snapshot_day={snapshot_day}）")
        symbols = list(
            db.scalars(
                select(UniverseMember.symbol)
                .where(UniverseMember.snapshot_id == snapshot.id)
                .where(UniverseMember.is_included.is_(True))
                .where(UniverseMember.trading_status == "active")
                .order_by(UniverseMember.symbol)
            ).all()
        )
    return (symbols[:limit] if limit else symbols), snapshot.trading_day.isoformat()


async def backfill(args: argparse.Namespace) -> int:
    if args.extend_calendar:
        with SessionLocal() as db:
            lookback = (date.today() - date.fromisoformat(args.start)).days + 120
            result = sync_trading_calendar(db, lookback_days=lookback, lookahead_days=200)
        print(f"交易日历：{result}")

    symbols, snapshot_day = resolve_symbols(
        date.fromisoformat(args.snapshot_day) if args.snapshot_day else None, args.limit
    )
    if not symbols:
        raise SystemExit("该快照没有可回补的标的")
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end) if args.end else datetime.now()
    print(f"股票池快照 {snapshot_day}：{len(symbols)} 只；区间 {start.date()} ~ {end.date()}；"
          f"数据源 {args.providers}；并发 {args.concurrency}")

    wanted = {name.strip() for name in args.providers.split(",") if name.strip()}
    providers = [p for p in _build_providers() if not wanted or getattr(p, "name", "") in wanted]
    if not providers:
        raise SystemExit(f"没有匹配的数据源：{args.providers}")
    print("实际数据源优先级：" + ", ".join(getattr(p, "name", "?") for p in providers))
    manager = ProviderManager(providers)
    for provider in providers:
        starter = getattr(provider, "start", None)
        if starter is not None:
            await starter()

    semaphore = asyncio.Semaphore(args.concurrency)
    failures: dict[str, str] = {}
    incomplete: dict[str, str] = {}
    added_total = 0
    bars_total = 0
    done = 0
    started = time.perf_counter()

    async def one(symbol: str) -> None:
        nonlocal added_total, bars_total, done
        async with semaphore:
            with SessionLocal() as db:
                service = HistoricalDataService(db, provider_manager=manager)
                try:
                    result = await service.get_history(symbol, start, end, adjust="none")
                except Exception as exc:  # noqa: BLE001 - 单只失败不能拖垮整批
                    failures[symbol] = str(exc)[:200]
                    done += 1
                    return
                bars_total += len(result.bars)
                if not result.is_complete:
                    incomplete[symbol] = f"缺 {len(result.quality.missing_dates)} 个交易日"
        done += 1
        if done % args.log_every == 0 or done == len(symbols):
            rate = done / max(time.perf_counter() - started, 1e-6)
            print(f"  {done}/{len(symbols)}  失败 {len(failures)}  不完整 {len(incomplete)}  "
                  f"{rate:.1f} 只/秒  累计 {time.perf_counter() - started:.0f}s", flush=True)

    await asyncio.gather(*(one(symbol) for symbol in symbols))
    for provider in providers:
        stopper = getattr(provider, "stop", None)
        if stopper is not None:
            await stopper()

    elapsed = time.perf_counter() - started
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_day": snapshot_day,
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "symbols": len(symbols),
        "providers": [getattr(p, "name", "?") for p in providers],
        "concurrency": args.concurrency,
        "elapsed_seconds": round(elapsed, 1),
        "bars_in_window": bars_total,
        "failed": failures,
        "incomplete": incomplete,
    }
    out = Path(args.out) if args.out else (
        ROOT / "outputs" / f"backfill_history_{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    print(f"\n完成：{len(symbols)} 只，{elapsed:.0f}s，区间内共 {bars_total} 根；"
          f"失败 {len(failures)}，不完整 {len(incomplete)}")
    print(f"报告：{out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回补多年日线历史（不复权口径）")
    parser.add_argument("--start", default=DEFAULT_START, help=f"区间起点，默认 {DEFAULT_START}")
    parser.add_argument("--end", default="", help="区间终点，默认今天")
    parser.add_argument("--snapshot-day", default="", help="指定股票池快照交易日，默认最近一次")
    parser.add_argument("--providers", default="eastmoney,tencent,akshare",
                        help="数据源优先级，默认绕开带全局锁的 tdx 以便并发")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 只（先量吞吐）")
    parser.add_argument("--log-every", type=int, default=100)
    parser.add_argument("--extend-calendar", action="store_true",
                        help="先把交易日历铺到区间起点（区间早于本地日历时必须加）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if args.limit <= 0:
        args.limit = None
    return asyncio.run(backfill(args))


if __name__ == "__main__":
    raise SystemExit(main())
