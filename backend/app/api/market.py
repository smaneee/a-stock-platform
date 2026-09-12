"""东方财富市场级数据接口：板块行情、板块成分股、资金流、数据中心报表。

只读接口，数据来自东方财富横截面接口（见 app.market_data.eastmoney_market）与
数据中心报表（见 app.market_data.eastmoney_datacenter）。
东财单 IP 限流较严：抓不到数据时返回 503，前端应提示「数据源暂不可用」而不是
显示空表格；服务内部已做主机故障转移与冷却，恢复后自动可用。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    get_datacenter_service,
    get_limit_up_service,
    get_market_service,
)
from app.database.models import LimitUpSentiment
from app.database.session import get_db
from app.market_data.eastmoney_datacenter import (
    EastmoneyDatacenterService,
    dataset_catalog,
)
from app.market_data.eastmoney_limit_up import (
    EastmoneyLimitUpService,
    pool_catalog,
)
from app.market_data.limit_up_store import LimitUpSentimentStore
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


class LimitUpFieldInfo(BaseModel):
    key: str
    title: str
    kind: str


class LimitUpPoolInfo(BaseModel):
    key: str
    label: str
    description: str
    fields: list[LimitUpFieldInfo]


class LimitUpCatalogResponse(BaseModel):
    count: int
    pools: list[LimitUpPoolInfo]


class LimitUpPoolResponse(BaseModel):
    pool: str
    label: str
    trade_date: str
    total: int
    page: int
    count: int
    items: list[dict[str, Any]]


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
    limit: int = Query(50, ge=1, le=1000),
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
    limit: int = Query(100, ge=1, le=1000),
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
    limit: int = Query(50, ge=1, le=1000),
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
    limit: int = Query(50, ge=1, le=1000),
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


@router.get("/limit-up", response_model=LimitUpCatalogResponse)
async def limit_up_catalog() -> LimitUpCatalogResponse:
    """涨停板情绪池目录（涨停 / 跌停 / 炸板 / 强势 / 次新）。"""
    pools = [LimitUpPoolInfo(**item) for item in pool_catalog()]
    return LimitUpCatalogResponse(count=len(pools), pools=pools)


class LimitUpSentimentRow(BaseModel):
    """涨停板情绪因子的一行（一个交易日）。"""

    trade_date: str
    limit_up_count: int
    limit_down_count: int
    broken_board_count: int
    strong_count: int
    sub_new_count: int
    seal_rate: float | None
    broken_rate: float | None
    max_streak: int
    first_board_count: int
    streak_2_count: int
    streak_3_count: int
    streak_4_count: int
    streak_5plus_count: int
    total_seal_amount: float
    total_limit_up_amount: float
    source: str
    captured_at: str


class LimitUpSentimentResponse(BaseModel):
    count: int
    start: str | None = Field(None, description="实际返回曲线的首个交易日（无数据为 null）")
    end: str | None = Field(None, description="实际返回曲线的最后一个交易日（无数据为 null）")
    latest: LimitUpSentimentRow | None
    items: list[LimitUpSentimentRow]


class LimitUpCaptureRequest(BaseModel):
    trade_date: date | None = Field(None, description="抓取哪个交易日；默认今天")
    backfill_days: int = Field(
        0, ge=0, le=90, description="大于 0 时改为回补最近 N 个自然日"
    )


class LimitUpCaptureResponse(BaseModel):
    count: int
    items: list[LimitUpSentimentRow]


def _sentiment_row(row: LimitUpSentiment) -> LimitUpSentimentRow:
    return LimitUpSentimentRow(
        trade_date=row.trade_date.isoformat(),
        limit_up_count=row.limit_up_count,
        limit_down_count=row.limit_down_count,
        broken_board_count=row.broken_board_count,
        strong_count=row.strong_count,
        sub_new_count=row.sub_new_count,
        seal_rate=row.seal_rate,
        broken_rate=row.broken_rate,
        max_streak=row.max_streak,
        first_board_count=row.first_board_count,
        streak_2_count=row.streak_2_count,
        streak_3_count=row.streak_3_count,
        streak_4_count=row.streak_4_count,
        streak_5plus_count=row.streak_5plus_count,
        total_seal_amount=row.total_seal_amount,
        total_limit_up_amount=row.total_limit_up_amount,
        source=row.source,
        captured_at=row.captured_at.isoformat(),
    )


@router.get("/limit-up/sentiment", response_model=LimitUpSentimentResponse)
async def limit_up_sentiment_history(
    start: date | None = Query(None, description="起始交易日（含）"),
    end: date | None = Query(None, description="结束交易日（含）"),
    limit: int = Query(120, ge=1, le=1000),
    db: Session = Depends(get_db),
    service: EastmoneyLimitUpService = Depends(get_limit_up_service),
) -> LimitUpSentimentResponse:
    """涨停板情绪因子历史曲线：封板率 / 连板高度 / 连板梯队。

    只有被定时任务（或手动 capture）落过库的交易日才有数据；本地尚未落库时
    返回空列表，请先调用 ``POST /api/market/limit-up/capture`` 抓取。
    """
    store = LimitUpSentimentStore(db, service)
    rows = store.history(start, end, limit)
    latest = store.latest()
    return LimitUpSentimentResponse(
        count=len(rows),
        start=rows[0].trade_date.isoformat() if rows else None,
        end=rows[-1].trade_date.isoformat() if rows else None,
        latest=_sentiment_row(latest) if latest is not None else None,
        items=[_sentiment_row(row) for row in rows],
    )


@router.post("/limit-up/capture", response_model=LimitUpCaptureResponse)
async def limit_up_capture(
    payload: LimitUpCaptureRequest,
    db: Session = Depends(get_db),
    service: EastmoneyLimitUpService = Depends(get_limit_up_service),
) -> LimitUpCaptureResponse:
    """抓取并落库涨停板情绪池（幂等：同一天重复抓取会覆盖）。

    ``backfill_days`` 大于 0 时回补最近 N 个自然日。上游只保留最近若干个
    交易日，更早的日期会返回空数据并被跳过，不会写入空行。
    """
    store = LimitUpSentimentStore(db, service)
    try:
        if payload.backfill_days > 0:
            await store.backfill(payload.backfill_days, end=payload.trade_date)
            reference = payload.trade_date or date.today()
            rows = store.history(
                start=reference - timedelta(days=payload.backfill_days),
                end=reference,
                limit=payload.backfill_days + 1,
            )
        else:
            result = await store.capture(payload.trade_date)
            if result is None:
                raise HTTPException(
                    status_code=404, detail="该交易日上游暂无情绪池数据"
                )
            rows = store.history(
                start=result.trade_date, end=result.trade_date, limit=1
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return LimitUpCaptureResponse(
        count=len(rows), items=[_sentiment_row(row) for row in rows]
    )


@router.get("/limit-up/{pool}", response_model=LimitUpPoolResponse)
async def limit_up_pool(
    pool: str,
    limit: int = Query(50, ge=1, le=200),
    page: int = Query(1, ge=1),
    order: str | None = Query(None, pattern="^(asc|desc)$"),
    trade_date: date | None = Query(None, description="交易日；默认最近交易日"),
    service: EastmoneyLimitUpService = Depends(get_limit_up_service),
) -> LimitUpPoolResponse:
    """单个情绪池的快照；上游只保留最近若干个交易日。"""
    try:
        result = await service.query(
            pool, limit=limit, page=page, order=order, trade_date=trade_date
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc)
    return LimitUpPoolResponse(
        pool=result.pool.key,
        label=result.pool.label,
        trade_date=result.trade_date,
        total=result.total,
        page=result.page,
        count=len(result.items),
        items=result.items,
    )


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
