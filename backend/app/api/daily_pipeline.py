"""Daily research pipeline endpoints."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DailyPipelineRun
from app.database.session import get_db
from app.selection import SelectionConfig
from app.tasks.daily_pipeline import (
    DailyPipelineError,
    DailyPipelineService,
    serialize_pipeline_run,
)

router = APIRouter(prefix="/api/daily-pipeline", tags=["daily-pipeline"])


class DailyPipelineCreate(BaseModel):
    trading_day: date
    paper_account_id: int | None = None
    auto_execute_paper: bool = False
    paper_validation_override: bool = False
    top_n: int = Field(default=5, ge=1, le=50)
    min_bars: int = Field(default=61, ge=61, le=500)
    lookback_days: int = Field(default=180, ge=61, le=1000)
    history_lookback_days: int = Field(default=365, ge=90, le=2000)
    max_stale_days: int = Field(default=10, ge=1, le=30)
    exclude_st: bool = True
    adjust: str = Field(default="none", pattern="^(none|qfq|hfq)$")


@router.post("/runs", status_code=202)
def create_daily_pipeline_run(
    body: DailyPipelineCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        run = DailyPipelineService(
            db, provider_manager=request.app.state.provider_manager
        ).create_run(
            body.trading_day,
            paper_account_id=body.paper_account_id,
            auto_execute_paper=body.auto_execute_paper,
            paper_validation_override=body.paper_validation_override,
            config=SelectionConfig(
                top_n=body.top_n,
                min_bars=body.min_bars,
                lookback_days=body.lookback_days,
                max_stale_days=body.max_stale_days,
                exclude_st=body.exclude_st,
                adjust=body.adjust,
            ),
            lookback_days=body.history_lookback_days,
        )
    except DailyPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_pipeline_run(run)


@router.get("/runs")
def list_daily_pipeline_runs(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    runs = db.scalars(
        select(DailyPipelineRun)
        .order_by(DailyPipelineRun.id.desc())
        .limit(limit)
    ).all()
    return {"items": [serialize_pipeline_run(run) for run in runs]}


@router.get("/runs/{run_id}")
def get_daily_pipeline_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(DailyPipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="日常流水线任务不存在")
    return serialize_pipeline_run(run)


@router.get("/schedule")
def get_daily_pipeline_schedule(request: Request) -> dict:
    """只读返回每日流水线自动调度配置（来自 .env，不在这里修改）。

    自动调度只创建研究/模拟任务，永不触发真实下单。
    """
    scheduler = getattr(request.app.state, "daily_pipeline_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="每日流水线自动调度未初始化")
    return scheduler.describe()
