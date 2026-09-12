"""东方财富市场级数据接口：板块行情、板块成分股、资金流、数据中心报表。

只读接口，数据来自东方财富横截面接口（见 app.market_data.eastmoney_market）与
数据中心报表（见 app.market_data.eastmoney_datacenter）。
东财单 IP 限流较严：抓不到数据时返回 503，前端应提示「数据源暂不可用」而不是
显示空表格；服务内部已做主机故障转移与冷却，恢复后自动可用。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from app.api.deps import get_datacenter_service, get_market_service
from app.market_data.eastmoney_datacenter import (
    EastmoneyDatacenterService,
    dataset_catalog,
)
from app.market_data.eastmoney_market import (
    BOARD_KINDS,
    BoardMember,
    BoardQuote,
    EastmoneyMarketService,
    FundFlowPoint,
    FundFlowRow,
)
from app.validation import validate_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["market"])


class BoardListResponse(BaseModel):
    kind: str
    count: int
    items: list[BoardQuote]


class BoardMemberResponse(BaseModel):
    board_code: str
    count: int
    items: list[BoardMember]


class FundFlowResponse(BaseModel):
    kind: str
    count: int
    items: list[FundFlowRow]


class StockFundFlowHistoryResponse(BaseModel):
    symbol: str
    count: int
    items: list[FundFlowPoint]


class DatacenterFieldInfo(BaseModel):
    key: str
    title: str
    kind: str


class DatacenterDatasetInfo(BaseModel):
    key: str
    label: str
    description: str
    supports_date: bool
    supports_symbol: bool
    fields: list[DatacenterFieldInfo]


class DatacenterCatalogResponse(BaseModel):
    count: int
    datasets: list[DatacenterDatasetInfo]


class DatacenterQueryResponse(BaseModel):
    dataset: str
    label: str
    total: int
    page: int
    count: int
    rows: list[dict[str, Any]]


class DragonTigerSeatsResponse(BaseModel):
    symbol: str
    trade_date: str | None = None
    buy: list[dict[str, Any]]
    sell: list[dict[str, Any]]


def _unavailable(exc: Exception) -> HTTPException:
    logger.warning("东方财富市场数据请求失败: %s", exc)
    return HTTPException(
        status_code=503, detail=f"东方财富数据源暂不可用: {exc}"
    )


@router.get("/boards", response_model=BoardListResponse)
async def list_boards(
    kind: str = Query("industry", description="industry / concept / region"),
    limit: int = Query(50, ge=1, le=500),
    order: str = Query("desc", pattern="^(desc|asc)$"),
    service: EastmoneyMarketService = Depends(get_market_service),
) -> BoardListResponse:
    """板块行情列表（默认按涨跌幅降序）。"""
    try:
        items = await service.list_boards(kind, limit=limit, order=order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return BoardListResponse(kind=kind, count=len(items), items=items)


@router.get("/boards/{board_code}/constituents", response_model=BoardMemberResponse)
async def board_constituents(
    board_code: str,
    limit: int = Query(100, ge=1, le=500),
    service: EastmoneyMarketService = Depends(get_market_service),
) -> BoardMemberResponse:
    """板块成分股（默认按涨跌幅降序）。"""
    try:
        items = await service.board_constituents(board_code, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return BoardMemberResponse(
        board_code=board_code.upper(), count=len(items), items=items
    )


@router.get("/fund-flow/boards", response_model=FundFlowResponse)
async def board_fund_flow(
    kind: str = Query("industry", description="industry / concept / region"),
    limit: int = Query(50, ge=1, le=500),
    order: str = Query("desc", pattern="^(desc|asc)$"),
    service: EastmoneyMarketService = Depends(get_market_service),
) -> FundFlowResponse:
    """板块资金流排行（默认按主力净流入降序）。"""
    try:
        items = await service.board_fund_flow(kind, limit=limit, order=order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return FundFlowResponse(kind=kind, count=len(items), items=items)


@router.get("/fund-flow/stocks", response_model=FundFlowResponse)
async def stock_fund_flow_rank(
    limit: int = Query(50, ge=1, le=500),
    order: str = Query("desc", pattern="^(desc|asc)$"),
    service: EastmoneyMarketService = Depends(get_market_service),
) -> FundFlowResponse:
    """个股资金流排行（默认按主力净流入降序）。"""
    try:
        items = await service.stock_fund_flow_rank(limit=limit, order=order)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return FundFlowResponse(kind="stock", count=len(items), items=items)


@router.get(
    "/fund-flow/stocks/{symbol}", response_model=StockFundFlowHistoryResponse
)
async def stock_fund_flow_history(
    symbol: str,
    days: int = Query(60, ge=1, le=1000),
    service: EastmoneyMarketService = Depends(get_market_service),
) -> StockFundFlowHistoryResponse:
    """个股资金流历史（按交易日升序）。"""
    if not validate_symbol(symbol):
        raise HTTPException(status_code=422, detail="非法股票代码")
    try:
        items = await service.stock_fund_flow_history(symbol, days=days)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return StockFundFlowHistoryResponse(
        symbol=symbol, count=len(items), items=items
    )


@router.get("/datacenter", response_model=DatacenterCatalogResponse)
async def datacenter_catalog() -> DatacenterCatalogResponse:
    """数据集目录与字段说明（前端据此渲染表头与筛选条件）。"""
    datasets = [DatacenterDatasetInfo(**item) for item in dataset_catalog()]
    return DatacenterCatalogResponse(count=len(datasets), datasets=datasets)


@router.get(
    "/datacenter/dragon-tiger/{symbol}/seats",
    response_model=DragonTigerSeatsResponse,
)
async def dragon_tiger_seats(
    symbol: str = Path(..., pattern=r"^\d{6}$", description="6 位股票代码"),
    trade_date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(50, ge=1, le=500),
    order: str | None = Query(None, pattern="^(asc|desc)$"),
    service: EastmoneyDatacenterService = Depends(get_datacenter_service),
) -> DragonTigerSeatsResponse:
    """龙虎榜买卖席位明细（按席位成交额排序）。"""
    try:
        seats = await service.dragon_tiger_seats(
            symbol, trade_date=trade_date, limit=limit, order=order
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return DragonTigerSeatsResponse(
        symbol=symbol.strip(), trade_date=trade_date, buy=seats["buy"], sell=seats["sell"]
    )


@router.get("/datacenter/{dataset}", response_model=DatacenterQueryResponse)
async def datacenter_query(
    dataset: str,
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="精确日期"),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    symbol: str | None = Query(None, pattern=r"^\d{6}$"),
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1, le=200),
    order: str | None = Query(None, pattern="^(asc|desc)$"),
    service: EastmoneyDatacenterService = Depends(get_datacenter_service),
) -> DatacenterQueryResponse:
    """查询东方财富数据中心数据集（龙虎榜/大宗交易/融资融券/沪深港通/机构调研…）。"""
    try:
        result = await service.query(
            dataset,
            date=date,
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            limit=limit,
            page=page,
            order=order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return DatacenterQueryResponse(
        dataset=result.spec.key,
        label=result.spec.label,
        total=result.total,
        page=page,
        count=len(result.rows),
        rows=result.rows,
    )


__all__ = ["router", "get_market_service", "BOARD_KINDS"]
