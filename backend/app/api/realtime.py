"""实时研究候选排序 API。

- ``GET /api/realtime/picks``  全市场扫描，给出研究候选排序与证据状态

只读接口：不写数据库、不改动任何现有信号与策略逻辑。全部计算只用 ``<= 信号日``
之前的数据（技术指标用最近 N 根日线，市场情绪按 ``sentiment_lag_days`` 滞后），
不含未来函数。数据源不可用时返回 503，前端应提示「数据源暂不可用」而不是显示空表。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_radar_validation_service, get_screener_service
from app.realtime.screener import (
    BOARD_LABELS,
    RISK_LABELS,
    ScreenerConfig,
    ScreenerResult,
    ScreenerService,
    ScreenerUnavailable,
)
from app.realtime.validation import (
    CostModel,
    RadarValidationService,
    ValidationConfig,
    ValidationReport,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/realtime", tags=["realtime"])

KNOWN_EVIDENCE_SOURCE = "docs/研发计划-2026Q4.md v1.1 §1.2"
KNOWN_PRIMARY_HORIZON = 3
KNOWN_MEAN_EXCESS_PCT = -0.390
KNOWN_EVIDENCE_SUMMARY = (
    "已登记的 6 折滚动样本外结果：3 日平均超额收益 -0.390%，仅 1/6 折为正；"
    "当前规则未通过，排名不得解释为买入建议。"
)


class ScreenerPickResponse(BaseModel):
    """单条买入候选。"""

    rank: int
    symbol: str
    name: str
    exchange: str
    board: str
    board_label: str
    price: float
    previous_close: float
    change_pct: float
    score: float
    strength_score: float = Field(
        0.0,
        description=(
            "连续强度分（0~100）= Σ 权重 × 该条件的连续强度；**排名以它为主键**。"
            "score 是命中条件的权重和（0/1 打分），同样命中 7 条的标的会并列（例如都是 86.0）。"
        ),
    )
    triggers: list[str]
    reasons: list[str]
    risk_flags: list[str]
    risk_labels: list[str]
    entry_low: float
    entry_high: float
    stop_loss: float
    target_price: float
    risk_reward: float
    suggested_weight_pct: float
    atr14: float
    atr_pct: float
    ma20: float
    ma60: float
    momentum_20: float
    momentum_60: float
    volatility_20: float
    amount_20: float
    volume_ratio: float
    rsi14: float
    kdj_k: float
    kdj_d: float
    macd_hist: float
    boll_upper: float
    boll_lower: float
    bar_count: int
    last_bar_date: str
    live: bool


class ScreenerSentimentResponse(BaseModel):
    """扫描时点的市场情绪读数（涨停板情绪因子）。"""

    available: bool
    sentiment_date: str | None
    seal_rate: float | None
    broken_rate: float | None
    max_streak: int | None
    limit_up_count: int | None
    exposure: float
    stance: str
    label: str
    note: str


class ScreenerConfigResponse(BaseModel):
    """本次扫描实际生效的参数。"""

    top_n: int
    lookback_days: int
    refine_pool: int
    exclude_st: bool
    min_amount_20: float
    min_price: float
    max_price: float
    min_change_pct: float
    max_change_pct: float
    min_triggers: int
    stop_atr_multiple: float
    target_atr_multiple: float
    risk_budget_pct: float
    max_weight_pct: float
    sentiment_lag_days: int
    scale_exposure_by_sentiment: bool


class ResearchEvidenceResponse(BaseModel):
    """当前排序能否升级为推荐的证据门禁。"""

    status: str = Field(
        ...,
        description=(
            "not_validated / validation_running / validation_failed / "
            "not_passed / insufficient_evidence"
        ),
    )
    label: str
    recommendation_allowed: bool = Field(
        ..., description="只有满足独立发布评审门槛时才可能为 true"
    )
    summary: str
    validation_task_state: str
    report_generated_at: str | None
    primary_horizon: int | None
    mean_excess_pct: float | None
    evidence_source: str


class ScreenerResponse(BaseModel):
    """一次扫描的完整结果。"""

    session_day: str = Field(..., description="行情所属交易日（最近交易日）")
    signal_day: str = Field(..., description="买点评估所针对的交易日")
    bars_last_day: str | None = Field(..., description="本地日线的最后一个交易日")
    bars_adjust: str = Field(..., description="本地日线复权口径：qfq（前复权）/ none（不复权）")
    live: bool = Field(..., description="是否有标的的行情带来了当日 K 线")
    generated_at: str = Field(..., description="扫描完成时间（UTC，历史字段，保留兼容）")
    generated_at_cst: str = Field("", description="扫描完成时间（北京时间 ISO8601，带 +08:00 偏移）")
    timezone: str = Field("Asia/Shanghai (UTC+8, 无夏令时)", description="时间基准")
    coverage_ratio: float = Field(0.0, description="行情覆盖率 = quoted / universe_size")
    coverage_ok: bool = Field(
        False, description="覆盖率是否足以支撑候选排序结论（False 时不得作为研究依据）"
    )
    scan_seconds: float
    universe_size: int
    quoted: int
    screened: int
    refined: int
    picks: list[ScreenerPickResponse]
    sentiment: ScreenerSentimentResponse
    evidence: ResearchEvidenceResponse
    notes: list[str]
    config: ScreenerConfigResponse


def _to_pick(item) -> ScreenerPickResponse:
    return ScreenerPickResponse(
        rank=item.rank,
        symbol=item.symbol,
        name=item.name,
        exchange=item.exchange,
        board=item.board,
        board_label=BOARD_LABELS.get(item.board, item.board),
        price=item.price,
        previous_close=item.previous_close,
        change_pct=item.change_pct,
        score=item.score,
        strength_score=item.strength_score,
        triggers=list(item.triggers),
        reasons=list(item.reasons),
        risk_flags=list(item.risk_flags),
        risk_labels=[RISK_LABELS.get(flag, flag) for flag in item.risk_flags],
        entry_low=item.entry_low,
        entry_high=item.entry_high,
        stop_loss=item.stop_loss,
        target_price=item.target_price,
        risk_reward=item.risk_reward,
        suggested_weight_pct=item.suggested_weight_pct,
        atr14=item.atr14,
        atr_pct=item.atr_pct,
        ma20=item.ma20,
        ma60=item.ma60,
        momentum_20=item.momentum_20,
        momentum_60=item.momentum_60,
        volatility_20=item.volatility_20,
        amount_20=item.amount_20,
        volume_ratio=item.volume_ratio,
        rsi14=item.rsi14,
        kdj_k=item.kdj_k,
        kdj_d=item.kdj_d,
        macd_hist=item.macd_hist,
        boll_upper=item.boll_upper,
        boll_lower=item.boll_lower,
        bar_count=item.bar_count,
        last_bar_date=item.last_bar_date.isoformat(),
        live=item.live,
    )


def _research_evidence(service: RadarValidationService) -> ResearchEvidenceResponse:
    """把验证器状态收敛成保守、不可误解的产品门禁。"""
    status = service.status()
    task_state = str(status.get("state", "idle"))
    report = service.report

    if report is None or report.control != "none":
        return ResearchEvidenceResponse(
            status="not_passed",
            label="当前未通过样本外验证",
            recommendation_allowed=False,
            summary=KNOWN_EVIDENCE_SUMMARY,
            validation_task_state=task_state,
            report_generated_at=None,
            primary_horizon=KNOWN_PRIMARY_HORIZON,
            mean_excess_pct=KNOWN_MEAN_EXCESS_PCT,
            evidence_source=KNOWN_EVIDENCE_SOURCE,
        )

    primary = next(
        (item for item in report.horizons if item.horizon == report.config.primary_horizon),
        None,
    )
    mean_excess = None if primary is None else primary.mean_excess_pct
    if mean_excess is not None and mean_excess <= 0:
        evidence_status = "not_passed"
        label = "未通过样本外验证"
        summary = (
            f"主口径 {report.config.primary_horizon} 日平均超额收益 "
            f"{mean_excess:.3f}%，未通过；排名不得解释为买入建议。"
        )
    else:
        evidence_status = "not_passed"
        label = "当前未通过样本外验证"
        summary = (
            f"单次报告主口径平均超额为 {mean_excess:.3f}%，但不能推翻已登记的 6 折负结果；"
            "且尚未满足预注册、多折独立留出、成本压力和置信区间等发布门槛。"
        )
    return ResearchEvidenceResponse(
        status=evidence_status,
        label=label,
        recommendation_allowed=False,
        summary=summary,
        validation_task_state=task_state,
        report_generated_at=report.generated_at,
        primary_horizon=report.config.primary_horizon,
        mean_excess_pct=mean_excess,
        evidence_source=KNOWN_EVIDENCE_SOURCE,
    )


def _to_response(
    result: ScreenerResult,
    validation_service: RadarValidationService,
) -> ScreenerResponse:
    sentiment = result.sentiment
    cfg = result.config
    return ScreenerResponse(
        session_day=result.session_day.isoformat(),
        signal_day=result.signal_day.isoformat(),
        bars_last_day=result.bars_last_day.isoformat() if result.bars_last_day else None,
        bars_adjust=result.bars_adjust,
        live=result.live,
        generated_at=result.generated_at,
        generated_at_cst=result.generated_at_cst,
        coverage_ratio=result.coverage_ratio,
        coverage_ok=result.coverage_ok,
        scan_seconds=result.scan_seconds,
        universe_size=result.universe_size,
        quoted=result.quoted,
        screened=result.screened,
        refined=result.refined,
        picks=[_to_pick(pick) for pick in result.picks],
        sentiment=ScreenerSentimentResponse(
            available=sentiment.available,
            sentiment_date=(
                sentiment.sentiment_date.isoformat()
                if sentiment.sentiment_date
                else None
            ),
            seal_rate=sentiment.seal_rate,
            broken_rate=sentiment.broken_rate,
            max_streak=sentiment.max_streak,
            limit_up_count=sentiment.limit_up_count,
            exposure=sentiment.exposure,
            stance=sentiment.stance,
            label=sentiment.label,
            note=sentiment.note,
        ),
        evidence=_research_evidence(validation_service),
        notes=list(result.notes),
        config=ScreenerConfigResponse(
            top_n=cfg.top_n,
            lookback_days=cfg.lookback_days,
            refine_pool=cfg.refine_pool,
            exclude_st=cfg.exclude_st,
            min_amount_20=cfg.min_amount_20,
            min_price=cfg.min_price,
            max_price=cfg.max_price,
            min_change_pct=cfg.min_change_pct,
            max_change_pct=cfg.max_change_pct,
            min_triggers=cfg.min_triggers,
            stop_atr_multiple=cfg.stop_atr_multiple,
            target_atr_multiple=cfg.target_atr_multiple,
            risk_budget_pct=cfg.risk_budget_pct,
            max_weight_pct=cfg.max_weight_pct,
            sentiment_lag_days=cfg.sentiment_lag_days,
            scale_exposure_by_sentiment=cfg.scale_exposure_by_sentiment,
        ),
    )


@router.get("/picks", response_model=ScreenerResponse)
async def realtime_picks(
    top_n: int = Query(10, ge=1, le=100, description="返回前 N 只候选"),
    refine_pool: int = Query(200, ge=1, le=3000, description="进入精算的候选池大小"),
    lookback_days: int = Query(120, ge=61, le=800, description="技术指标窗口根数"),
    exclude_st: bool = Query(True, description="是否剔除 ST / *ST"),
    min_amount_20: float = Query(
        50_000_000.0, ge=0, description="20 日日均成交额下限（元）"
    ),
    min_price: float = Query(2.0, gt=0, description="最低股价（元）"),
    max_price: float = Query(1000.0, gt=0, description="最高股价（元）"),
    min_change_pct: float = Query(-7.0, description="当日涨跌幅下限（%）"),
    max_change_pct: float = Query(7.0, description="当日涨跌幅上限（%）"),
    min_triggers: int = Query(3, ge=0, le=8, description="至少命中几个触发器"),
    stop_atr_multiple: float = Query(2.0, gt=0, description="下行风险线 = 参考价 - N×ATR"),
    target_atr_multiple: float = Query(3.0, gt=0, description="上行情景线 = 参考价 + N×ATR"),
    risk_budget_pct: float = Query(
        1.0, gt=0, le=100, description="单笔可承受的账户风险（%）"
    ),
    max_weight_pct: float = Query(20.0, gt=0, le=100, description="单只最高仓位（%）"),
    sentiment_lag_days: int = Query(
        1, ge=1, description="情绪滞后交易日数（至少 1，避免未来函数）"
    ),
    scale_exposure_by_sentiment: bool = Query(
        True, description="是否按封板率缩放模型风险预算"
    ),
    service: ScreenerService = Depends(get_screener_service),
    validation_service: RadarValidationService = Depends(get_radar_validation_service),
) -> ScreenerResponse:
    """全市场实时扫描：给出研究候选排序，不构成买入推荐。

    两段式：先用便宜因子把全市场筛到 ``refine_pool`` 只，再对入围池精算
    RSI / BOLL / KDJ / MACD / ATR 并打分。行情来自多连接并行的通达信实时行情，
    本地日线取最近 ``lookback_days`` 根不复权日线（默认 120，与「技术指标」页
    250 根口径实测偏差 RSI<0.1、ATR<0.1%）。

    休市日不会伪造当日 K 线：行情与日线逐字段一致时按最近交易日收盘数据评估，
    买点针对下一交易日；市场情绪按 ``sentiment_lag_days`` 滞后取值。

    本地数据缺失（交易日历 / 股票池 / 行情全失败）返回 503。
    """
    config = ScreenerConfig(
        top_n=top_n,
        lookback_days=lookback_days,
        refine_pool=refine_pool,
        exclude_st=exclude_st,
        min_amount_20=min_amount_20,
        min_price=min_price,
        max_price=max_price,
        min_change_pct=min_change_pct,
        max_change_pct=max_change_pct,
        min_triggers=min_triggers,
        stop_atr_multiple=stop_atr_multiple,
        target_atr_multiple=target_atr_multiple,
        risk_budget_pct=risk_budget_pct,
        max_weight_pct=max_weight_pct,
        sentiment_lag_days=sentiment_lag_days,
        scale_exposure_by_sentiment=scale_exposure_by_sentiment,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = await service.run(config)
    except ScreenerUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "screener_unavailable", "message": str(exc)},
        ) from exc
    return _to_response(result, validation_service)


# ─────────────────── 样本外验证（walk-forward） ───────────────────


class ValidationCostResponse(BaseModel):
    """本次验证实际使用的交易成本。"""

    commission_rate: float
    stamp_tax_rate: float
    slippage_bps: float
    round_trip_pct: float


class ValidationHorizonResponse(BaseModel):
    """单个持有期的样本外统计。"""

    horizon: int = Field(..., description="持有交易日数")
    observations: int = Field(..., description="有效样本数（按候选计）")
    mean_net_pct: float = Field(..., description="扣成本后平均收益（%）")
    median_net_pct: float
    hit_rate: float = Field(..., description="扣成本后收益为正的比例")
    mean_benchmark_pct: float = Field(..., description="同池等权基准平均收益（%）")
    mean_excess_pct: float = Field(..., description="平均超额收益（%，候选 - 基准）")
    excess_daily_mean_pct: float
    excess_t_stat: float = Field(
        ..., description="按日度超额序列算的单样本 t（窗口重叠，会偏高）"
    )
    excess_days: int
    max_excess_drawdown_pct: float = Field(..., description="日度超额复利累乘的最大回撤（%）")


class ValidationBucketResponse(BaseModel):
    """按命中分数分层的超额收益（主口径持有期）。"""

    label: str
    observations: int
    mean_excess_pct: float
    hit_rate: float


class ValidationDailyResponse(BaseModel):
    """单个评估日的记录（主口径持有期）。"""

    signal_day: str
    eligible: int
    picks: int
    tradable: int
    symbols: list[str]
    net_pct: float | None
    benchmark_pct: float | None
    excess_pct: float | None


class ValidationReportResponse(BaseModel):
    """一次样本外验证的完整结果。"""

    generated_at: str
    first_signal_day: str | None
    last_signal_day: str | None
    evaluation_days: int
    universe_size: int
    bars_adjust: str = Field(..., description="本次使用的日线复权口径：qfq / none")
    bars_first_day: str | None
    bars_last_day: str | None
    elapsed_seconds: float
    control: str = Field(..., description="none / random / worst")
    primary_horizon: int = Field(..., description="主口径持有期（交易日）")
    horizons: list[ValidationHorizonResponse]
    score_buckets: list[ValidationBucketResponse]
    daily: list[ValidationDailyResponse]
    skipped_not_tradable: int
    skipped_no_outcome: int
    costs: ValidationCostResponse
    caveats: list[str]
    notes: list[str]


class ValidationStatusResponse(BaseModel):
    """验证任务的运行状态。"""

    state: str = Field(..., description="idle / running / done / failed")
    progress: dict[str, int]
    started_at: str | None
    finished_at: str | None
    error: str | None
    has_report: bool
    report_generated_at: str | None


class ValidationEnvelopeResponse(BaseModel):
    """状态 + 最近一次报告。"""

    status: ValidationStatusResponse
    report: ValidationReportResponse | None


def _to_validation(report: ValidationReport) -> ValidationReportResponse:
    costs = report.config.costs
    return ValidationReportResponse(
        generated_at=report.generated_at,
        first_signal_day=(
            report.first_signal_day.isoformat() if report.first_signal_day else None
        ),
        last_signal_day=(
            report.last_signal_day.isoformat() if report.last_signal_day else None
        ),
        evaluation_days=report.evaluation_days,
        universe_size=report.universe_size,
        bars_adjust=report.bars_adjust,
        bars_first_day=(
            report.bars_first_day.isoformat() if report.bars_first_day else None
        ),
        bars_last_day=(
            report.bars_last_day.isoformat() if report.bars_last_day else None
        ),
        elapsed_seconds=report.elapsed_seconds,
        control=report.control,
        primary_horizon=report.config.primary_horizon,
        horizons=[
            ValidationHorizonResponse(**vars(item)) for item in report.horizons
        ],
        score_buckets=[
            ValidationBucketResponse(**vars(item)) for item in report.score_buckets
        ],
        daily=[
            ValidationDailyResponse(
                signal_day=item.signal_day.isoformat(),
                eligible=item.eligible,
                picks=item.picks,
                tradable=item.tradable,
                symbols=list(item.symbols),
                net_pct=item.net_pct,
                benchmark_pct=item.benchmark_pct,
                excess_pct=item.excess_pct,
            )
            for item in report.daily
        ],
        skipped_not_tradable=report.skipped_not_tradable,
        skipped_no_outcome=report.skipped_no_outcome,
        costs=ValidationCostResponse(**costs.as_dict()),
        caveats=list(report.caveats),
        notes=list(report.notes),
    )


@router.get(
    "/picks/validation",
    response_model=ValidationEnvelopeResponse,
    summary="买点雷达样本外验证结果",
)
async def get_picks_validation(
    service: RadarValidationService = Depends(get_radar_validation_service),
) -> ValidationEnvelopeResponse:
    """返回验证任务状态与最近一次报告（报告在内存里，重启后需重算）。"""
    report = service.report
    return ValidationEnvelopeResponse(
        status=ValidationStatusResponse(**service.status()),
        report=None if report is None else _to_validation(report),
    )


@router.post(
    "/picks/validation",
    status_code=202,
    response_model=ValidationEnvelopeResponse,
    summary="启动买点雷达样本外验证",
)
async def start_picks_validation(
    eval_days: int = Query(60, ge=5, le=240, description="评估多少个交易日"),
    horizons: str = Query("1,3,5,10", description="持有期（交易日，逗号分隔）"),
    primary_horizon: int = Query(3, ge=1, le=60, description="主口径持有期"),
    top_n: int = Query(10, ge=1, le=100, description="每日等权买入的候选数"),
    min_amount_20: float = Query(
        50_000_000.0, ge=0, description="20 日日均成交额下限（元）"
    ),
    min_triggers: int = Query(3, ge=0, le=8, description="至少命中几个触发器"),
    commission_rate: float = Query(0.0003, ge=0, le=0.01, description="佣金费率（单边）"),
    stamp_tax_rate: float = Query(0.0005, ge=0, le=0.01, description="印花税率（仅卖出）"),
    slippage_bps: float = Query(5.0, ge=0, le=100, description="单边滑点（基点）"),
    control: str = Query(
        "none",
        pattern="^(none|random|worst)$",
        description="none=按打分选股；random=同池等量随机样本（噪声底噪）；worst=同池最差样本",
    ),
    service: RadarValidationService = Depends(get_radar_validation_service),
) -> ValidationEnvelopeResponse:
    """把买点雷达的规则放到历史上逐日重放，检验样本外是否真的有超额收益。

    打分只用 ``<= t`` 的日线；成交按 ``t+1`` 开盘买入、``t+1+k`` 收盘卖出，
    扣除双边佣金、印花税与滑点；基准是同日同池等权收益。次日停牌或开盘即涨停
    计为买不到。``control=random`` 是脚手架自检：其超额收益应接近 0。

    计算在后台线程执行（60 个评估日约 100 秒），本接口立即返回 202 与状态，
    前端轮询 ``GET /api/realtime/picks/validation``。同一时刻只跑一个任务。
    """
    try:
        parsed = tuple(int(part) for part in horizons.split(",") if part.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="horizons 必须是逗号分隔的整数"
        ) from exc
    config = ValidationConfig(
        eval_days=eval_days,
        horizons=parsed,
        primary_horizon=primary_horizon,
        costs=CostModel(
            commission_rate=commission_rate,
            stamp_tax_rate=stamp_tax_rate,
            slippage_bps=slippage_bps,
        ),
        screener=replace(
            ScreenerConfig(),
            top_n=top_n,
            min_amount_20=min_amount_20,
            min_triggers=min_triggers,
        ),
        control=control,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    status = await service.start(config)
    report = service.report
    return ValidationEnvelopeResponse(
        status=ValidationStatusResponse(**status),
        report=None if report is None else _to_validation(report),
    )
