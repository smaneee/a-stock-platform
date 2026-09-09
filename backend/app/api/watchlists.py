"""自选股接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Watchlist, WatchlistSymbol
from app.database.session import get_db
from app.validation import validate_symbol

router = APIRouter(prefix="/api", tags=["watchlists"])


class WatchlistCreate(BaseModel):
    name: str


class SymbolAdd(BaseModel):
    symbol: str
    name: str | None = None


@router.get("/watchlists")
def list_watchlists(db: Session = Depends(get_db)) -> list[dict]:
    """列出所有自选股列表。"""
    watchlists = db.scalars(select(Watchlist)).all()
    result = []
    for wl in watchlists:
        symbols = [
            {"symbol": s.symbol, "name": s.name}
            for s in wl.symbols
        ]
        result.append({"id": wl.id, "name": wl.name, "symbols": symbols})
    return result


@router.post("/watchlists", status_code=201)
def create_watchlist(body: WatchlistCreate, db: Session = Depends(get_db)) -> dict:
    """创建自选股列表。"""
    watchlist = Watchlist(name=body.name)
    db.add(watchlist)
    db.commit()
    db.refresh(watchlist)
    return {"id": watchlist.id, "name": watchlist.name, "symbols": []}


@router.post("/watchlists/{watchlist_id}/symbols")
def add_symbol(
    watchlist_id: int,
    body: SymbolAdd,
    db: Session = Depends(get_db),
) -> dict:
    """向自选股列表添加股票。"""
    if not validate_symbol(body.symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")

    watchlist = db.get(Watchlist, watchlist_id)
    if watchlist is None:
        raise HTTPException(status_code=404, detail="自选股列表不存在")

    existing = db.scalars(
        select(WatchlistSymbol).where(
            WatchlistSymbol.watchlist_id == watchlist_id,
            WatchlistSymbol.symbol == body.symbol,
        )
    ).first()
    if existing:
        return {"symbol": existing.symbol, "name": existing.name}

    symbol = WatchlistSymbol(
        watchlist_id=watchlist_id,
        symbol=body.symbol,
        name=body.name,
    )
    db.add(symbol)
    db.commit()
    return {"symbol": symbol.symbol, "name": symbol.name}


@router.delete("/watchlists/{watchlist_id}/symbols/{symbol}")
def remove_symbol(
    watchlist_id: int,
    symbol: str,
    db: Session = Depends(get_db),
) -> dict:
    """从自选股列表移除股票。"""
    entry = db.scalars(
        select(WatchlistSymbol).where(
            WatchlistSymbol.watchlist_id == watchlist_id,
            WatchlistSymbol.symbol == symbol,
        )
    ).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="股票不在列表中")
    db.delete(entry)
    db.commit()
    return {"deleted": symbol}
