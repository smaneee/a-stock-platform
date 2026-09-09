"""健康检查接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_provider_manager

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    """服务健康检查，附带数据源健康状态。"""
    provider_manager = request.app.state.provider_manager
    provider_status = await provider_manager.health_check()
    return {
        "status": "ok",
        "disclaimer": "分析结果仅用于研究，不构成投资建议。",
        "providers": provider_status,
    }
