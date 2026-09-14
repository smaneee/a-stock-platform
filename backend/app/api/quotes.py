"""行情接口。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_intraday_service, get_provider_manager
from app.database.models import Security
from app.database.session import get_db
from app.market_data.provider_manager import ProviderManager
from app.realtime.intraday import DEFAULT_LIMIT, IntradaySeries, IntradayService
from app.validation import sanitize_symbols, validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["quotes"])


class BatchQuotesRequest(BaseModel):
    symbols: list[str] = Field(..., description="股票代码列表")


class QuoteResponse(BaseModel):
    symbol: str
    name: str
    price: float
    open: float
    high: float
    low: float
    previous_close: float
    volume: float
    amount: float
    bid_price: float
    ask_price: float
    source: str
    market_time: str | None = None
    received_at: str
    is_stale: bool


def _to_response(quote) -> dict:
    return QuoteResponse(
        symbol=quote.symbol,
        name=quote.name,
        price=quote.price,
        open=quote.open,
        high=quote.high,
        low=quote.low,
        previous_close=quote.previous_close,
        volume=quote.volume,
        amount=quote.amount,
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        source=quote.source,
        market_time=quote.market_time.isoformat() if quote.market_time else None,
        received_at=quote.received_at.isoformat(),
        is_stale=quote.is_stale,
    ).model_dump()


@router.get("/quotes/{symbol}")
async def get_quote(
    symbol: str,
    provider_manager: ProviderManager = Depends(get_provider_manager),
) -> dict:
    """获取单只股票实时行情。"""
    if not validate_symbol(symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    quote = await provider_manager.get_quote(symbol)
    if quote is None:
        raise HTTPException(status_code=404, detail="未获取到行情")
    return _to_response(quote)


@router.post("/quotes/batch")
async def batch_quotes(
    body: BatchQuotesRequest,
    provider_manager: ProviderManager = Depends(get_provider_manager),
) -> dict:
    """批量获取行情。"""
    symbols = sanitize_symbols(body.symbols)
    if not symbols:
        raise HTTPException(status_code=422, detail="无合法股票代码")
    quotes = await provider_manager.get_quotes(symbols)
    return {"quotes": [_to_response(q) for q in quotes.values() if q is not None]}


@router.get("/quotes/{symbol}/intraday")
async def get_intraday(
    symbol: str,
    limit: int = Query(DEFAULT_LIMIT, ge=30, le=2000, description="返回的最大点数"),
    intraday_service: IntradayService = Depends(get_intraday_service),
    db: Session = Depends(get_db),
) -> dict:
    """当日分时曲线（实时）。

    - 实时分钟线来自内存缓存，只覆盖自选股，随轮询（默认 3 秒）原地刷新；
    - 程序启动前已经走完的时段用数据源的 1 分钟分时补齐（任意标的都行）。

    非交易日（周末 / 节假日）返回的是**最近一个有数据的交易日**整段分时，
    不会返回空曲线。``source`` 说明这一条是实时、基线还是两者合并。
    """
    if not validate_symbol(symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    series = await intraday_service.get(symbol, limit=limit)
    if not series.points:
        raise HTTPException(status_code=404, detail="未获取到当日分时数据")
    return _intraday_payload(series, db)


def _security_name(db: Session, symbol: str) -> str:
    """本地股票主数据里的名称；查不到或查询失败都返回空串。

    名称只是展示信息，读主数据失败不该让整条曲线不可用。
    """
    try:
        return db.scalar(select(Security.name).where(Security.symbol == symbol)) or ""
    except Exception:  # noqa: BLE001
        logger.warning("读取股票名称失败: %s", symbol, exc_info=True)
        return ""


def _intraday_payload(series: IntradaySeries, db: Session) -> dict:
    """序列化分时曲线；数据源没给名称时退化到本地股票主数据。"""
    payload = series.to_dict()
    if payload["name"] == series.symbol:
        payload["name"] = _security_name(db, series.symbol) or payload["name"]
    return payload
