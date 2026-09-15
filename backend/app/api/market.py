"""东方财富市场级数据接口：板块行情、板块成分股、资金流、数据中心报表。

只读接口，数据来自东方财富横截面接口（见 app.market_data.eastmoney_market）与
数据中心报表（见 app.market_data.eastmoney_datacenter）。
东财单 IP 限流较严：抓不到数据时返回 503，前端应提示「数据源暂不可用」而不是
显示空表格；服务内部已做主机故障转移与冷却，恢复后自动可用。
"""
from __future__ import annotations

import logging
from datetime import UTC, date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    get_datacenter_service,
    get_limit_up_service,
    get_market_service,
)
from app.config import get_settings
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
from app.market_data.indices import DEFAULT_BENCHMARKS, list_indices
from app.market_data.sentiment_backfill import SentimentBackfillService
from app.market_data.eastmoney_market import (
    BOARD_KINDS,
    BoardMember,
    BoardQuote,
    EastmoneyMarketService,
    FundFlowPoint,
    FundFlowRow,
)
from app.market_rules.calendar import TradingCalendar
from app.market_rules.session_state import (
    CST,
    candidate_label,
    now_cst,
    resolve_phase,
    to_cst,
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
    #: 是否套用了数据集默认视角（"upcoming" = 限售解禁「最近将解禁」），
    #: 让前端能如实告诉用户「你看到的不是全部历史，而是未来区间」。
    applied_default: str | None = None


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


class IndexItem(BaseModel):
    """可作为回测基准的指数。"""

    symbol: str  # 规范代码，如 sh000300
    code: str  # 6 位代码
    name: str  # 中文简称


class IndexListResponse(BaseModel):
    count: int
    default: str
    items: list[IndexItem]


@router.get("/indices", response_model=IndexListResponse)
async def list_benchmark_indices() -> IndexListResponse:
    """可做基准的指数列表（本地登记表，不依赖外部数据源）。"""
    items = [IndexItem(**row) for row in list_indices()]
    return IndexListResponse(
        count=len(items), default=DEFAULT_BENCHMARKS[0], items=items
    )


class MarketSessionResponse(BaseModel):
    """当前市场时段与「此刻能不能成交」的完整状态（P0-02）。

    与 ``/api/universe`` 的「可交易」不同：股票池的 is_included 是排除规则的结果
    （退市/停牌/ST），休市日也不会变；本接口回答的是**此刻**能否成交，并在休市时
    明确把候选标记为「下一交易日研究候选」。
    """

    day: str
    is_trading_day: bool
    last_trading_day: str
    next_trading_day: str | None
    calendar_total: int
    # ── P0-02 新增 ──
    server_time: str = Field(..., description="服务器当前北京时间（带 +08:00 偏移）")
    timezone: str = Field("Asia/Shanghai (UTC+8, 无夏令时)", description="时间基准")
    phase: str = Field(..., description="pre_open/call_auction/morning/noon_break/afternoon/closed/non_trading_day")
    phase_label: str = Field(..., description="时段中文名")
    phase_note: str = Field(..., description="该时段的含义与限制")
    is_open: bool = Field(..., description="此刻是否存在连续竞价（可成交）")
    orders_accepted: bool = Field(..., description="此刻交易所是否接受委托")
    tradable_now: bool = Field(..., description="是否可以在此刻真实成交")
    candidates_are: str = Field(..., description="候选语义：盘中实时 / 下一交易日研究候选")
    universe: dict = Field(default_factory=dict, description="在册可交易标的状态统计")
    account_permissions: dict = Field(default_factory=dict, description="本机账户/通道权限（实盘默认关闭）")


@router.get("/session", response_model=MarketSessionResponse)
async def market_session(
    day: date | None = Query(None, description="查询哪一天，默认今天"),
    db: Session = Depends(get_db),
) -> MarketSessionResponse:
    """今天是否交易日、最近交易日与下一交易日。

    与 /api/universe 的「可交易」无关：那个是股票池排除规则的结果（退市 /
    停牌 / 长期停牌 / ST），休市日不会变化；本接口回答的是「今天能不能成交」，
    用来消除「休市日池子还显示可交易」的误解。
    """
    calendar = TradingCalendar(db)
    if calendar.is_empty():
        raise HTTPException(
            status_code=503,
            detail={"error": "calendar_unavailable", "message": "本地交易日历为空"},
        )
    target = day or date.today()
    is_trading_day = calendar.is_trading_day(target)
    try:
        last_day = calendar.last_trading_day_on_or_before(target)
    except ValueError as exc:
        # 目标日前 30 天内没有任何交易日：日历覆盖不足，按不可用处理
        raise HTTPException(
            status_code=503,
            detail={"error": "calendar_coverage", "message": str(exc)},
        ) from exc
    try:
        next_day: date | None = calendar.next_trading_day(last_day)
    except ValueError:
        next_day = None

    phase = resolve_phase(None, is_trading_day and target == now_cst().date())
    return MarketSessionResponse(
        day=target.isoformat(),
        is_trading_day=is_trading_day,
        last_trading_day=last_day.isoformat(),
        next_trading_day=next_day.isoformat() if next_day else None,
        calendar_total=calendar.count(),
        server_time=now_cst().isoformat(timespec="seconds"),
        phase=phase.key,
        phase_label=phase.label,
        phase_note=phase.note,
        is_open=phase.tradable,
        orders_accepted=phase.orders_accepted,
        tradable_now=phase.tradable,
        candidates_are=candidate_label(phase, last_day.isoformat(), next_day.isoformat() if next_day else None),
        universe=_universe_snapshot_stats(db),
        account_permissions=_account_permissions(),
    )


def _universe_snapshot_stats(db: Session) -> dict:
    """最近快照的在册标的状态统计（含停牌/ST/退市/待上市）。

    没有快照时返回 ``snapshot_day=None`` 并说明原因，绝不返回 0 冒充「没有标的」。
    """
    from sqlalchemy import func, select

    from app.database.models import UniverseMember, UniverseSnapshot

    latest = db.execute(
        select(UniverseSnapshot).order_by(UniverseSnapshot.trading_day.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return {
            "snapshot_day": None,
            "note": "本地尚无股票池快照，请先在「股票池」页触发一次同步。",
        }
    rows = db.execute(
        select(
            UniverseMember.is_included,
            UniverseMember.trading_status,
            func.count().label("n"),
        )
        .where(UniverseMember.snapshot_id == latest.id)
        .group_by(UniverseMember.is_included, UniverseMember.trading_status)
    ).all()
    by_status: dict[str, int] = {}
    included = 0
    excluded = 0
    for is_included, status, n in rows:
        key = (status or "unknown").strip() or "unknown"
        by_status[key] = by_status.get(key, 0) + int(n)
        if is_included:
            included += int(n)
        else:
            excluded += int(n)
    st_count = db.execute(
        select(func.count())
        .select_from(UniverseMember)
        .where(UniverseMember.snapshot_id == latest.id, UniverseMember.is_st.is_(True))
    ).scalar_one()
    return {
        "snapshot_day": latest.trading_day.isoformat(),
        "total": included + excluded,
        "included": included,
        "excluded": excluded,
        "st_count": int(st_count),
        "by_trading_status": by_status,
        "note": "included=通过排除规则的在册标的，不等于此刻可下单；能否成交看 tradable_now。",
    }


def _account_permissions() -> dict:
    """本机通道权限：实盘默认关闭，模拟盘可用。不含任何账号/密钥信息。"""
    from app.config import get_settings

    settings = get_settings()
    live = bool(settings.real_trading_enabled)
    return {
        "live_trading_enabled": live,
        "live_trading_reason": (
            "REAL_TRADING_ENABLED=true（仍受一次性确认令牌与逐笔人工确认约束）"
            if live
            else "实盘开关默认关闭（REAL_TRADING_ENABLED=false），当前只能研究与模拟"
        ),
        "paper_trading_available": True,
        "research_only_disclaimer": "所有分析、选股与回测结果仅用于研究，不构成投资建议。",
    }


def _serialize_quote_freshness(quote, phase, symbol: str, elapsed_ms: float, threshold: float) -> dict:
    """行情新鲜度信封（P0-02）：源时间 / 接收时间 / 来源 / 耗时 / 行情年龄 / 是否过期。

    关键约束：**不把接收时间冒充交易所时间**。源行情时间缺失时，
    ``source_time`` 为 None、``quote_age_seconds`` 为 None 且 ``age_status=unknown``。
    休市时段不计算行情年龄（否则会显示成「行情老了 8 小时」这类误导性数字）。
    """
    from app.market_data.base import QuoteData  # noqa: F401  (仅用于类型说明)

    source_time = to_cst(getattr(quote, "market_time", None))
    received = getattr(quote, "received_at", None)
    # QuoteData.received_at 默认来自 utc_now()：数据库约定是 UTC naive。
    # to_cst() 对普通 naive 时间按“已是北京时间”处理，因此这里必须先显式补 UTC，
    # 否则会把 12:55 UTC 错标成 12:55+08:00，而正确值应为 20:55+08:00。
    received_cst = (
        received.replace(tzinfo=UTC).astimezone(CST)
        if received is not None and received.tzinfo is None
        else to_cst(received)
    )
    age = None
    age_status = "unknown"
    if source_time is not None and phase.tradable:
        age = max(0.0, (now_cst() - source_time).total_seconds())
        age_status = "known"
    elif source_time is None:
        age_status = "source_time_missing"
    else:
        age_status = "not_applicable_market_not_open"
    return {
        "symbol": symbol,
        "source": getattr(quote, "source", "") or "unknown",
        "source_time": source_time.isoformat(timespec="seconds") if source_time else None,
        "source_time_status": "known" if source_time else "unknown",
        "received_at": received_cst.isoformat(timespec="seconds") if received_cst else None,
        "server_time": now_cst().isoformat(timespec="seconds"),
        "request_elapsed_ms": round(elapsed_ms, 1),
        "quote_age_seconds": round(age, 1) if age is not None else None,
        "quote_age_status": age_status,
        "stale_threshold_seconds": threshold,
        "is_stale": bool(getattr(quote, "is_stale", False)),
        "market_phase": phase.key,
        "client_display_note": (
            "客户端展示时间由浏览器本地时钟渲染；请与 server_time 比对判断时钟偏差，"
            "不要用接收时间代替交易所时间。"
        ),
    }


@router.get("/freshness")
async def market_freshness(
    request: Request,
    symbol: str = Query("600519", description="用于探测行情新鲜度的标的"),
    db: Session = Depends(get_db),
) -> dict:
    """单标的行情新鲜度信封：来源、源时间、接收时间、耗时、行情年龄、是否过期。"""
    import time as _time

    calendar = TradingCalendar(db)
    target = date.today()
    is_trading_day = (not calendar.is_empty()) and calendar.is_trading_day(target)
    phase = resolve_phase(None, is_trading_day and target == now_cst().date())
    threshold = get_settings().max_quote_age_seconds

    provider_manager = getattr(request.app.state, "provider_manager", None)
    quote = None
    error = None
    started = _time.perf_counter()
    if provider_manager is not None:
        try:
            quote = await provider_manager.get_quote(symbol)
        except Exception as exc:  # noqa: BLE001 - 数据源失败要如实返回而不是 500
            error = f"{type(exc).__name__}: {exc}"
    else:
        error = "provider_manager 未初始化"
    elapsed_ms = (_time.perf_counter() - started) * 1000

    payload: dict = {
        "symbol": symbol,
        "phase": phase.key,
        "phase_label": phase.label,
        "tradable_now": phase.tradable,
        "server_time": now_cst().isoformat(timespec="seconds"),
        "provider_error": error,
    }
    if quote is None:
        payload.update(
            {
                "source": "unknown",
                "source_time": None,
                "source_time_status": "unknown",
                "received_at": None,
                "request_elapsed_ms": round(elapsed_ms, 1),
                "quote_age_seconds": None,
                "quote_age_status": "unavailable",
                "stale_threshold_seconds": threshold,
                "is_stale": True,
                "note": "未取得任何数据源行情；行情年龄标为未知，不得据此生成交易意图。",
            }
        )
    else:
        payload.update(
            _serialize_quote_freshness(quote, phase, symbol, elapsed_ms, threshold)
        )
    return payload


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
    coverage_symbols: int | None = Field(
        None, description="当日纳入统计的标的数；东财实时榜单口径为 null"
    )
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


class LimitUpSentimentBackfillItem(BaseModel):
    """回算出的单日情绪因子（本次计算值）。"""

    trade_date: str
    limit_up_count: int
    limit_down_count: int
    broken_board_count: int
    coverage_symbols: int
    seal_rate: float | None
    broken_rate: float | None
    max_streak: int
    first_board_count: int
    streak_2_count: int
    streak_3_count: int
    streak_4_count: int
    streak_5plus_count: int


class LimitUpSentimentBackfillRequest(BaseModel):
    start: date | None = Field(None, description="回算起始交易日，缺省取本地日线最早一天")
    end: date | None = Field(None, description="回算结束交易日，缺省取本地日线最晚一天")
    coverage_floor: int = Field(
        1000,
        ge=0,
        le=100_000,
        description="样本面下限；低于该值的交易日整日不写入，避免家数失真",
    )
    include_st: bool = Field(
        False, description="是否把 ST / *ST 计入样本；东财涨停板池不含 ST，默认同为 false"
    )
    overwrite_derived: bool = Field(
        False, description="是否覆盖已有回算行；东财实时抓取的行永不被覆盖"
    )
    dry_run: bool = Field(False, description="只计算不写库，用于先核对口径")


class LimitUpSentimentBackfillResponse(BaseModel):
    start: str
    end: str
    symbols: int
    bars_scanned: int
    inserted: int
    updated: int
    skipped_existing: int
    skipped_low_coverage: int
    deleted_stale: int
    coverage_floor: int
    include_st: bool
    overwrite_derived: bool
    dry_run: bool
    days: int
    low_coverage_days: list[str]
    stale_days: list[str]
    items: list[LimitUpSentimentBackfillItem]


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
        coverage_symbols=row.coverage_symbols,
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

    未来日期一律拒绝（422）：落库任务绝不能写入未来交易日的情绪行。
    """
    if payload.trade_date is not None and payload.trade_date > now_cst().date():
        raise HTTPException(
            status_code=422,
            detail=(
                f"trade_date={payload.trade_date.isoformat()} 晚于今天，拒绝抓取/落库；"
                "情绪数据只能来自已经发生的交易日。"
            ),
        )
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


@router.post(
    "/limit-up/sentiment/backfill",
    response_model=LimitUpSentimentBackfillResponse,
)
def limit_up_sentiment_backfill(
    payload: LimitUpSentimentBackfillRequest,
    db: Session = Depends(get_db),
) -> LimitUpSentimentBackfillResponse:
    """用本地日线离线回算历史涨停情绪，补齐回测所需的历史情绪序列。

    与 ``POST /api/market/limit-up/capture`` 的分工：

    - 本接口**不打网络**，只读本地 ``historical_bars`` + 交易日历 + 涨跌停规则，
      因此可以一次性覆盖整年；capture 依赖东财上游，只能回溯最近若干个交易日。
    - 已由东财实时抓取写入的交易日默认跳过；``overwrite_derived=true`` 也只覆盖
      回算行，不会覆盖东财行。
    - 默认剔除 ST / *ST，与东财涨停板池口径对齐，避免两条曲线在接缝处出现台阶。

    计算口径与局限见 ``app.market_data.sentiment_backfill`` 模块文档。同步实现，
    由 FastAPI 放入线程池执行，不阻塞事件循环。
    """
    try:
        report = SentimentBackfillService(db).backfill(
            start=payload.start,
            end=payload.end,
            coverage_floor=payload.coverage_floor,
            include_st=payload.include_st,
            overwrite_derived=payload.overwrite_derived,
            dry_run=payload.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LimitUpSentimentBackfillResponse(**report.to_dict())


@router.get("/limit-up/{pool}", response_model=LimitUpPoolResponse)
async def limit_up_pool(
    pool: str,
    limit: int = Query(50, ge=1, le=200),
    page: int = Query(1, ge=1),
    order: str | None = Query(None, pattern="^(asc|desc)$"),
    trade_date: date | None = Query(None, description="交易日；默认最近交易日"),
    service: EastmoneyLimitUpService = Depends(get_limit_up_service),
) -> LimitUpPoolResponse:
    """单个情绪池的快照；上游只保留最近若干个交易日。

    未来日期直接拒绝（422）：上游对超出保留窗口的未来日期会返回「最新快照」，
    若照抄请求参数做标记，就会把 2026-09-11 的数据记成未来交易日（未来函数）。
    响应体里的 ``trade_date`` 始终以上游实际返回的 ``qdate`` 为准。
    """
    if trade_date is not None and trade_date > now_cst().date():
        raise HTTPException(
            status_code=422,
            detail=(
                f"trade_date={trade_date.isoformat()} 晚于今天，情绪池不接受未来日期；"
                "请用 /api/market/session 取最近交易日。"
            ),
        )
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
        applied_default=result.applied_default,
    )


__all__ = ["router", "get_market_service", "BOARD_KINDS"]
