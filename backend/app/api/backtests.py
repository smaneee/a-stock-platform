"""历史回测接口。"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backtest.engine import BacktestEngine
from app.database.models import Backtest
from app.database.session import SessionLocal, get_db
from app.strategies import registry
from app.validation import validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["backtests"])


class BacktestRequest(BaseModel):
    symbol: str
    strategy_name: str
    start_time: datetime
    end_time: datetime
    initial_cash: float = Field(100_000.0, gt=0)


@router.post("/backtests", status_code=202)
async def create_backtest(
    body: BacktestRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """创建回测任务，立即返回任务 ID（后台异步执行）。"""
    if not validate_symbol(body.symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    if body.start_time >= body.end_time:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    strategy = registry.get_strategy(body.strategy_name)
    if strategy is None:
        raise HTTPException(status_code=404, detail="策略不存在")

    backtest = Backtest(
        symbol=body.symbol,
        strategy_name=body.strategy_name,
        start_time=body.start_time,
        end_time=body.end_time,
        initial_cash=body.initial_cash,
        status="PENDING",
    )
    db.add(backtest)
    db.commit()
    db.refresh(backtest)

    provider_manager = request.app.state.provider_manager
    asyncio.create_task(
        _run_backtest(backtest.id, body, provider_manager)
    )
    return {"id": backtest.id, "status": "PENDING"}


@router.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: int, db: Session = Depends(get_db)) -> dict:
    """查询回测结果。"""
    backtest = db.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=404, detail="回测任务不存在")
    result = json.loads(backtest.result) if backtest.result else None
    return {
        "id": backtest.id,
        "symbol": backtest.symbol,
        "strategy_name": backtest.strategy_name,
        "start_time": backtest.start_time.isoformat(),
        "end_time": backtest.end_time.isoformat(),
        "status": backtest.status,
        "result": result,
    }


async def _run_backtest(backtest_id: int, body: BacktestRequest, provider_manager) -> None:
    """后台执行回测。"""
    db = SessionLocal()
    try:
        backtest = db.get(Backtest, backtest_id)
        if backtest is None:
            return
        backtest.status = "RUNNING"
        db.commit()

        # 获取历史数据
        history = await provider_manager.get_history(
            body.symbol, "daily", body.start_time, body.end_time
        )
        if not history:
            backtest.status = "FAILED"
            backtest.result = '{"error": "未获取到历史数据"}'
            db.commit()
            return

        # 运行回测
        strategy = registry.get_strategy(body.strategy_name)
        engine = BacktestEngine(strategy=strategy, initial_cash=body.initial_cash)
        result = engine.run(history)

        backtest.status = "DONE"
        backtest.result = json.dumps(result.to_dict(), ensure_ascii=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("回测失败: %s", exc)
        db.rollback()
        try:
            backtest = db.get(Backtest, backtest_id)
            if backtest:
                backtest.status = "FAILED"
                backtest.result = json.dumps(
                    {"error": "回测执行失败，请检查服务日志"}, ensure_ascii=False
                )
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
