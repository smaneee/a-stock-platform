"""universe 模块 API 路由。

端点：
- GET   /api/universe/status
      → 数据源健康、最近快照时间
- POST  /api/universe/sync
      → 触发异步同步（CI 友好，不阻塞）
- GET   /api/universe/snapshots
      → 历史快照列表（按 trading_day 倒序）
- GET   /api/universe/snapshots/{trading_day}/members
      → 某快照成员（支持 exchange / include_only / exclude_reasons 过滤）
- GET   /api/universe/filter
      → 当前可用筛选 query（按 trading_day 查，供 selection 调用）

阶段 1 约束：真实订单仍需用户确认；本模块仅与 selection 和自动模拟交易联通。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date as date_cls, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.universe.providers import ProviderError
from app.universe.snapshot_service import UniverseSnapshotService
from app.universe.sync_service import AllProvidersFailedError, UniverseSyncService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/universe", tags=["universe"])


def get_db() -> Session:
    """FastAPI 依赖：每个请求一个 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────── status ───────────


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)) -> dict:
    """返回数据源健康 + 最近一次同步信息。"""
    sync_svc = UniverseSyncService(db)
    snap_svc = UniverseSnapshotService(db)
    return {
        "provider_health": sync_svc.get_provider_health(),
        "latest_snapshot_date": (
            snap_svc.latest_snapshot_date().isoformat()
            if snap_svc.latest_snapshot_date()
            else None
        ),
    }


# ─────────── sync ───────────


@router.post("/sync")
async def sync(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """触发一次同步并落库。

    同步是异步调用（provider fetch_all 内部可能 await 网络调用）。
    失败 → 抛 503。所有 provider 失败 → 503，附 partial provider 状态。
    """
    sync_svc = UniverseSyncService(db)
    try:
        result = await sync_svc.sync()
    except AllProvidersFailedError as exc:
        raise HTTPException(
            status_code=503, detail={
                "error": "all_providers_failed",
                "message": str(exc),
                "provider_health": sync_svc.get_provider_health(),
            }
        )
    except ProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "provider_error",
                "message": str(exc),
            },
        )
    return result.as_dict()


# ─────────── snapshots ───────────


@router.get("/snapshots")
def list_snapshots(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    db: Session = Depends(get_db),
) -> dict:
    snap_svc = UniverseSnapshotService(db)
    snapshots = snap_svc.list_snapshots(limit=limit)
    return {"snapshots": snapshots}


@router.get("/snapshots/{trading_day}/members")
def get_members(
    request: Request,
    trading_day: Annotated[str, Path(description="交易日 YYYY-MM-DD")],
    include_only: Annotated[bool | None, Query()] = True,
    exchange: Annotated[
        str | None, Query(description="SH / SZ / BJ")
    ] = None,
    exclude_reason: Annotated[
        list[str] | None, Query(description="可重复传，例 ?exclude_reason=delisted&exclude_reason=suspended")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 5000,
    db: Session = Depends(get_db),
) -> dict:
    try:
        td = date_cls.fromisoformat(trading_day)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_date", "message": f"{trading_day} 不是合法 YYYY-MM-DD"},
        )
    snap_svc = UniverseSnapshotService(db)
    members = snap_svc.get_membership(
        td,
        include_only=include_only,
        exchange=exchange,
        exclude_reasons=exclude_reason,
        limit=limit,
    )
    return {
        "trading_day": td.isoformat(),
        "include_only": include_only,
        "exchange": exchange,
        "exclude_reasons": list(exclude_reason) if exclude_reason else None,
        "count": len(members),
        "members": [
            {
                "symbol": m.symbol,
                "name": m.name,
                "exchange": m.exchange,
                "is_st": m.is_st,
                "is_included": m.is_included,
                "exclude_reason": m.exclude_reason,
                "sort_rank": m.sort_rank,
            }
            for m in members
        ],
    }


# ─────────── filter (helper for selection + paper trading) ───────────


@router.post("/filter")
def filter_universe(
    request: Request,
    db: Session = Depends(get_db),
    trading_day: date_cls | None = None,
    include_only: bool = True,
    exchange: str | None = None,
) -> dict:
    """对当前最新快照做一次过滤，返回 symbol 列表。

    用于 selection 和 paper trading 的快速调用。
    若 trading_day 为空则用最新快照；该 trading_day 必须存在，否则 404。
    """
    snap_svc = UniverseSnapshotService(db)
    if trading_day is None:
        td = snap_svc.latest_snapshot_date()
        if td is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "no_snapshot", "message": "还没有任何 snapshot，请先 POST /api/universe/sync"},
            )
    else:
        td = trading_day

    members = snap_svc.get_membership(
        td,
        include_only=include_only,
        exchange=exchange,
        limit=10000,
    )
    return {
        "trading_day": td.isoformat(),
        "count": len(members),
        "symbols": [m.symbol for m in members],
    }
