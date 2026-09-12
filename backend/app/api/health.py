"""健康检查接口。

按 K8s 探针语义拆分：
- /api/health/live：进程存活，永远 200。
- /api/health/ready：就绪状态，DB 可用 + 调度器已启动才返回 200。
- /api/health：详细信息，包含数据源状态、监控/排障用。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.database.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


def _check_database() -> tuple[bool, str | None]:
    """执行 SELECT 1 验证数据库连接。"""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        db.close()


@router.get("/health/live")
def liveness() -> dict:
    """进程存活探针，永远返回 ok（除非进程崩溃）。"""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(request: Request) -> dict:
    """就绪探针：DB 可用 + 行情调度器已启动 + 交易日历非空才接流量。"""
    db_ok, db_error = _check_database()
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = bool(scheduler and scheduler.is_running)
    settlement_scheduler = getattr(request.app.state, "settlement_scheduler", None)
    settlement_running = bool(settlement_scheduler and settlement_scheduler.is_running)

    # 交易日历非空：否则未同步，禁止静默启动
    from app.market_rules.calendar import TradingCalendar
    from app.database.session import SessionLocal
    calendar_db = SessionLocal()
    try:
        calendar = TradingCalendar(calendar_db)
        calendar_ready = not calendar.is_empty()
        calendar_total = calendar.count()
    finally:
        calendar_db.close()

    ready = db_ok and scheduler_running and settlement_running and calendar_ready
    body = {
        "status": "ok" if ready else "not_ready",
        "database": db_ok,
        "scheduler": scheduler_running,
        "settlement_scheduler": settlement_running,
        "trading_calendar": {
            "ready": calendar_ready,
            "total": calendar_total,
        },
    }
    if db_error:
        body["database_error"] = db_error
    if not ready:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=body)
    return body


@router.get("/health")
async def health(request: Request) -> dict:
    """详细健康状态，含数据源健康与组件状态。"""
    provider_manager = request.app.state.provider_manager
    provider_status = await provider_manager.health_check()
    db_ok, db_error = _check_database()
    scheduler = getattr(request.app.state, "scheduler", None)
    settlement_scheduler = getattr(request.app.state, "settlement_scheduler", None)

    from app.market_rules.calendar import TradingCalendar
    from app.database.session import SessionLocal
    calendar_db = SessionLocal()
    try:
        calendar = TradingCalendar(calendar_db)
        calendar_info = {"total": calendar.count()}
    finally:
        calendar_db.close()

    return {
        "status": "ok",
        "disclaimer": "分析结果仅用于研究，不构成投资建议。",
        "database": {"ok": db_ok, **({"error": db_error} if db_error else {})},
        "scheduler": {"running": bool(scheduler and scheduler.is_running)},
        "settlement_scheduler": {"running": bool(settlement_scheduler and settlement_scheduler.is_running)},
        "trading_calendar": calendar_info,
        "providers": provider_status,
    }
