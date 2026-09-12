"""行情接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_provider_manager
from app.market_data.provider_manager import ProviderManager
from app.validation import sanitize_symbols, validate_symbol

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
