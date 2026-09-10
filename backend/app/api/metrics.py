"""可观测性接口。

返回数据源指标（成功率/延迟/连续失败/最后成功时间）、WebSocket 连接数、
回测任务累计结果，以及整体数据源状态判定。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.database.models import Backtest
from app.database.session import get_db
from app.observability.metrics import data_source_status, metrics

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
def metrics_endpoint(request: Request, db=Depends(get_db)) -> dict:
    """返回完整可观测性快照。"""
    provider_names = [p.name for p in request.app.state.provider_manager.providers]
    provider_metrics = metrics.provider_metrics()

    # 实时任务计数（队列深度等）
    status_counts = {
        row[0]: row[1]
        for row in db.execute(
            select(Backtest.status, func.count(Backtest.id)).group_by(Backtest.status)
        ).all()
    }

    return {
        "data_status": data_source_status(provider_metrics, provider_names),
        "providers": provider_metrics,
        "websocket": metrics.ws_metrics(),
        "tasks": {
            "live": status_counts,
            "cumulative": metrics.task_metrics(),
        },
    }
