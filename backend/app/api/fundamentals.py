"""投资决策辅助接口：基本面快照、企业质量、三情景估值。

设计约束（对应用户要求）：

* **数字全部来自确定性代码**，接口不做任何"看起来像结论"的编造；缺失即 ``null``；
* 每个响应都带 **数据时点**（``snapshot_date``/``report_date``/``fetched_at``）、**来源**
  （``source``）与**未验证项**说明，前端必须能展示这些字段；
* **投资吸引力（质量分）与证据置信度分开返回**，且都明确标注"不是上涨概率"；
* 允许返回 ``insufficient_evidence``/``stop_reason``（证据不足或数据过期 → 暂不行动）；
* 估值接口**不猜假设**：折现率、增长率、自由现金流率必须由调用方显式给出（默认值只用于
  演示，且在响应里标注为"示例假设，须由使用者替换"）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.models import FundamentalSnapshot
from app.database.session import get_db
from app.config import get_settings
from app.explain.deepseek import (
    DeepSeekExplainer,
    ExplainerConfig,
    build_evidence_pack,
)
from app.fundamentals.decision import (
    DecisionInputs,
    PortfolioContext,
    build_analysis,
    model_applicability,
)
from app.fundamentals.eastmoney_fundamentals import (
    FundamentalSnapshotData,
    annualization_factor,
    fetch_market_fundamentals,
)
from app.fundamentals.quality import (
    QualityInputs,
    assess_quality,
    evidence_confidence,
    profile_for,
)
from app.fundamentals.repository import (
    apply_statement_detail,
    beijing_today,
    coverage,
    industry_peers,
    latest_snapshot,
    snapshot_history,
    upsert_snapshots,
)
from app.fundamentals.statement_details import (
    StatementDetail,
    StatementDetailError,
    fetch_statement_detail,
)
from app.fundamentals.valuation import (
    ValuationAssumptions,
    ValuationError,
    implied_revenue_growth,
    intrinsic_value,
    margin_of_safety,
    scenario_band,
    sensitivity_table,
    upside_ratio,
)
from app.market_rules.session_state import now_cst

router = APIRouter(prefix="/api/fundamentals", tags=["fundamentals"])

DISCLAIMER = "以上为研究与数据分析结果，不构成投资建议，也不承诺任何收益。"


def _serialize(row: FundamentalSnapshot) -> dict:
    """快照 → 响应。派生值单独一组，并标注它们是模型推断。"""
    data = FundamentalSnapshotData(
        symbol=row.symbol,
        name=row.name,
        price=row.price or 0.0,
        market_cap=row.market_cap or 0.0,
        float_market_cap=row.float_market_cap,
        pe_dynamic=row.pe_dynamic,
        pb=row.pb,
        roe=row.roe,
        revenue=row.revenue,
        revenue_yoy=row.revenue_yoy,
        net_profit_parent=row.net_profit_parent,
        net_profit_yoy=row.net_profit_yoy,
        gross_margin=row.gross_margin,
        debt_ratio=row.debt_ratio,
        industry=row.industry,
        eps_diluted=row.eps_diluted,
        bps=row.bps,
        net_margin=row.net_margin,
        equity=row.equity,
        report_date=row.report_date,
        source=row.source,
    )
    try:
        warnings = json.loads(row.warnings or "[]")
    except (TypeError, ValueError):
        warnings = [f"告警字段无法解析: {row.warnings!r}"]
    statement = _statement_detail_from_row(row)
    return {
        **data.to_dict(),
        "snapshot_date": row.snapshot_date.isoformat(),
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "currency": "CNY",
        "derived": {
            "shares_outstanding": data.shares_outstanding,
            "annualized_revenue": data.annualized_revenue,
            "annualized_net_profit": data.annualized_net_profit,
            "annualization_factor": annualization_factor(row.report_date),
            "pe_from_statements": data.pe_from_statements,
            "pb_from_statements": data.pb_from_statements,
            "ps_annualized": data.ps_annualized,
            "origin": "模型推断（由报告期数据年化后计算），非上游直接给出",
        },
        "statement_detail": (
            {
                **statement.to_dict(revenue=row.revenue, net_profit=row.net_profit_parent),
                "fetched_at": (
                    row.statement_fetched_at.isoformat()
                    if row.statement_fetched_at else None
                ),
            }
            if statement is not None else None
        ),
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _statement_detail_from_row(row: FundamentalSnapshot) -> StatementDetail | None:
    """只使用与当前基本面快照同报告期的三表，避免时点错配。"""
    if (
        row.statement_report_date is None
        or row.statement_report_date != row.report_date
        or not row.statement_source
    ):
        return None
    return StatementDetail(
        symbol=row.symbol,
        report_date=row.statement_report_date,
        operating_cash_flow=row.operating_cash_flow,
        capital_expenditure=row.capital_expenditure,
        monetary_funds=row.monetary_funds,
        short_loan=row.short_loan,
        long_loan=row.long_loan,
        bonds_payable=row.bonds_payable,
        noncurrent_liab_due_year=row.noncurrent_liab_due_year,
        lease_liabilities=row.lease_liabilities,
        goodwill=row.goodwill,
        statement_equity=row.statement_equity,
        source=row.statement_source,
    )


@router.get("/coverage")
def get_coverage(db: Session = Depends(get_db)) -> dict:
    """快照覆盖统计：做任何横截面分析前先看这里（有没有数据、是不是同一天）。"""
    stats = coverage(db)
    return {
        **stats,
        "as_of_cst": now_cst().isoformat(),
        "note": (
            "覆盖率不足或抓取日不一致时，行业相对分位与横截面比较不可用；"
            "本接口只报告事实，不做插补"
        ),
    }


class RefreshRequest(BaseModel):
    max_pages: int = Field(default=80, ge=1, le=200, description="分页上限，实测单页 100 行")
    page_size: int = Field(default=100, ge=1, le=100)


@router.post("/refresh")
async def refresh_snapshot(
    payload: RefreshRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """抓取全市场基本面快照并入库（只读上游、写本平台库）。

    只有**全部页都成功**才把 ``complete=True``；否则如实返回 ``failures``，
    交由调用方决定是否使用（半份数据做横截面比较会产生系统性偏差）。
    """
    request = payload or RefreshRequest()
    snapshots, stats = await fetch_market_fundamentals(
        max_pages=request.max_pages, page_size=request.page_size
    )
    if not snapshots:
        raise HTTPException(
            status_code=502,
            detail=(
                "未取到任何基本面数据（可能上游限流或全部主机不可用）；"
                f"失败信息：{stats.failures[:3]} 不做任何入库，也不返回空结论"
            ),
        )
    result = upsert_snapshots(db, snapshots)
    fetched = stats.to_dict()
    return {
        "fetched": fetched,
        "stored": result.to_dict(),
        "coverage": coverage(db),
        "note": (
            "complete 指**取回**是否完整（无失败页且取回行数≥上游总数）；"
            "rows_skipped 是被有意跳过的行（无现价/无市值，如停牌股），不算取漏。"
            "coverage_ratio（解析/上游总数）低于 1 时做横截面比较要留意口径"
        ),
    }


def _quality_inputs(row: FundamentalSnapshot, as_of: date) -> QualityInputs:
    statement = _statement_detail_from_row(row)
    statement_values = (
        statement.to_dict(revenue=row.revenue, net_profit=row.net_profit_parent)
        if statement is not None else {}
    )
    return QualityInputs(
        roe=row.roe,
        gross_margin=row.gross_margin,
        net_margin=row.net_margin,
        debt_ratio=row.debt_ratio,
        revenue_yoy=row.revenue_yoy,
        profit_yoy=row.net_profit_yoy,
        ocf_to_profit=statement_values.get("ocf_to_profit"),
        goodwill_to_equity=statement_values.get("goodwill_to_equity"),
        report_date=row.report_date,
        as_of=as_of,
        industry=row.industry,
        currency="CNY",
        source=row.source,
    )


def _quality_and_confidence(row: FundamentalSnapshot) -> tuple[dict, dict, date]:
    as_of = beijing_today()
    assessment = assess_quality(_quality_inputs(row, as_of))
    confidence = evidence_confidence(
        coverage=assessment.coverage,
        report_date=row.report_date,
        as_of=as_of,
        industry_identified=bool(row.industry),
        has_statement_detail=_statement_detail_from_row(row) is not None,
        sources=2 if _statement_detail_from_row(row) is not None else 1,
    )
    return assessment.to_dict(), confidence.to_dict(), as_of


def _require_snapshot(db: Session, symbol: str) -> FundamentalSnapshot:
    row = latest_snapshot(db, symbol)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"没有 {symbol} 的基本面快照。请先 POST /api/fundamentals/refresh 抓取，"
                "本接口**不会**在没有数据时返回任何估值或结论（缺数据就是缺数据）"
            ),
        )
    return row


@router.get("/{symbol}")
def get_symbol(symbol: str, db: Session = Depends(get_db)) -> dict:
    """单标的：快照 + 企业质量 + 证据置信度（**不含**估值结论）。"""
    row = _require_snapshot(db, symbol)
    quality, confidence, as_of = _quality_and_confidence(row)
    return {
        "snapshot": _serialize(row),
        "quality": quality,
        "evidence_confidence": confidence,
        "as_of_cst": as_of.isoformat(),
        "notes": [
            "估值需要显式假设，请调用 POST /api/fundamentals/{symbol}/valuation；"
            "本接口不替使用者假设增长率或折现率",
            "质量分与证据置信度必须分开展示，二者都不是上涨概率",
        ],
        "disclaimer": DISCLAIMER,
    }


@router.post("/{symbol}/statement-detail/refresh")
async def refresh_statement_detail(
    symbol: str, db: Session = Depends(get_db)
) -> dict:
    """按标的刷新同报告期资产负债表与现金流量表，并返回派生口径。"""
    row = _require_snapshot(db, symbol)
    try:
        detail = await fetch_statement_detail(symbol)
    except StatementDetailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if row.report_date and detail.report_date != row.report_date:
        raise HTTPException(
            status_code=409,
            detail=(
                f"三表报告期 {detail.report_date} 与快照报告期 {row.report_date} 不一致；"
                "为避免时点错配，本次不入库"
            ),
        )
    apply_statement_detail(db, row, detail)
    quality, confidence, _ = _quality_and_confidence(row)
    return {
        "symbol": row.symbol,
        "name": row.name,
        "statement_detail": {
            **detail.to_dict(revenue=row.revenue, net_profit=row.net_profit_parent),
            "fetched_at": row.statement_fetched_at.isoformat()
            if row.statement_fetched_at else None,
        },
        "quality": quality,
        "evidence_confidence": confidence,
        "disclaimer": DISCLAIMER,
    }


class ValuationRequest(BaseModel):
    """显式估值假设。``basis`` 必填 —— 必须写明这些数字是事实、估计还是模型推断。"""

    revenue: float = Field(gt=0, description="基期营业总收入（元）；口径由 revenue_basis 决定")
    revenue_growth: float = Field(description="年化增长率（小数）")
    fcf_margin: float = Field(description="自由现金流 / 营业总收入（小数）")
    discount_rate: float = Field(description="折现率（小数）")
    terminal_growth: float = Field(description="永续增长率（小数）")
    shares: float = Field(gt=0, description="总股本（股）")
    net_debt: float = Field(default=0.0, description="有息负债 − 现金（元）")
    years: int = Field(default=5, ge=1, le=15)
    basis: str = Field(min_length=1, description="假设来源说明（事实/估计/模型推断）")
    #: 口径声明：报告期值（如半年报）还是已年化值。默认为报告期值 → 服务端按报告期年化
    revenue_basis: Literal["report_period", "annualized"] = "report_period"
    bear_overrides: dict[str, float] = Field(default_factory=dict)
    bull_overrides: dict[str, float] = Field(default_factory=dict)
    sensitivity_growth: list[float] | None = None
    sensitivity_discount: list[float] | None = None

    def to_assumptions(self, *, annualization: float = 1.0) -> ValuationAssumptions:
        revenue = self.revenue
        basis = self.basis
        if self.revenue_basis == "report_period" and annualization != 1.0:
            revenue = self.revenue * annualization
            basis = f"{basis}；营收口径：报告期值 ×{annualization:g} 年化（模型推断，未计季节性）"
        return ValuationAssumptions(
            revenue=revenue,
            revenue_growth=self.revenue_growth,
            fcf_margin=self.fcf_margin,
            discount_rate=self.discount_rate,
            terminal_growth=self.terminal_growth,
            shares=self.shares,
            net_debt=self.net_debt,
            years=self.years,
            basis=basis,
        )


@router.post("/{symbol}/valuation")
def post_valuation(
    symbol: str, payload: ValuationRequest, db: Session = Depends(get_db)
) -> dict:
    """三情景估值 + 敏感性。假设全部由调用方给出，算不出来就说明原因。"""
    row = latest_snapshot(db, symbol)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有 {symbol} 的快照，无法对照现价（先刷新快照）",
        )
    try:
        factor = annualization_factor(row.report_date) or 1.0
        base = payload.to_assumptions(annualization=factor)
        band = scenario_band(
            base,
            bear_overrides=payload.bear_overrides,
            bull_overrides=payload.bull_overrides,
        )
    except ValuationError as exc:
        raise HTTPException(status_code=422, detail=f"假设不成立：{exc}") from exc
    if band.base is None:
        # 基准情景都算不出来 → 这是调用方的假设问题，明确报错而不是返回空结果
        raise HTTPException(
            status_code=422,
            detail=f"基准情景假设不成立：{band.errors.get('基准', '未知原因')}",
        )

    upside = {
        scenario.label: upside_ratio(scenario.output.per_share, row.price)
        for scenario in (band.bear, band.base, band.bull)
        if scenario is not None
    }
    result = {
        **band.to_dict(),
        "price": row.price,
        "upside_vs_price": upside,
        "margin_of_safety_base": margin_of_safety(
            band.base.output.per_share if band.base else None, row.price
        ),
        "basis": payload.basis,
        "basis_note": "以上假设由调用方提供；本平台不代为假设，也不保证其合理性",
        "revenue_caliber": {
            "declared": payload.revenue_basis,
            "annualization_factor_applied": factor,
            "revenue_used": base.revenue,
            "note": (
                "报告期营收（如半年报）直接被当成年度基数会把价值低估约一半 —— "
                "所以这里按报告期自动年化并把系数写在结果里；要跳过请传 revenue_basis=annualized"
            ),
        },
        "applicability": model_applicability(profile_for(row.industry)[0].key),
        "disclaimer": DISCLAIMER,
    }
    if payload.sensitivity_growth and payload.sensitivity_discount:
        result["sensitivity"] = sensitivity_table(
            base,
            growth_values=payload.sensitivity_growth,
            discount_values=payload.sensitivity_discount,
        )
    return result


class ReverseValuationRequest(BaseModel):
    """反向估值输入：固定其余假设，只反解现价隐含的收入增长率。"""

    revenue: float = Field(gt=0, description="基期营业总收入（元）")
    fcf_margin: float = Field(gt=0, description="自由现金流 / 营业总收入（小数）")
    discount_rate: float = Field(gt=0, lt=0.5, description="折现率（小数）")
    terminal_growth: float = Field(description="永续增长率（小数）")
    shares: float = Field(gt=0, description="总股本（股）")
    net_debt: float = Field(default=0.0, description="有息负债 − 现金（元）")
    years: int = Field(default=5, ge=1, le=15)
    basis: str = Field(min_length=1, description="固定假设来源说明")
    revenue_basis: Literal["report_period", "annualized"] = "report_period"
    lower_growth: float = Field(default=-0.30, gt=-0.5, lt=1.0)
    upper_growth: float = Field(default=0.60, gt=-0.5, lt=1.0)

    def to_assumptions(self, *, annualization: float) -> ValuationAssumptions:
        revenue = self.revenue
        basis = self.basis
        if self.revenue_basis == "report_period" and annualization != 1.0:
            revenue *= annualization
            basis = f"{basis}；营收口径：报告期值 ×{annualization:g} 年化（模型推断，未计季节性）"
        return ValuationAssumptions(
            revenue=revenue,
            revenue_growth=0.0,
            fcf_margin=self.fcf_margin,
            discount_rate=self.discount_rate,
            terminal_growth=self.terminal_growth,
            shares=self.shares,
            net_debt=self.net_debt,
            years=self.years,
            basis=basis,
        )


@router.post("/{symbol}/reverse-valuation")
def post_reverse_valuation(
    symbol: str, payload: ReverseValuationRequest, db: Session = Depends(get_db)
) -> dict:
    """回答“当前价格隐含了什么增长”，而不是再给一个目标价。"""
    row = _require_snapshot(db, symbol)
    try:
        factor = annualization_factor(row.report_date) or 1.0
        base = payload.to_assumptions(annualization=factor)
        result = implied_revenue_growth(
            base,
            target_price=float(row.price),
            lower_bound=payload.lower_growth,
            upper_bound=payload.upper_growth,
        )
    except ValuationError as exc:
        raise HTTPException(status_code=422, detail=f"假设不成立：{exc}") from exc
    return {
        **result,
        "symbol": row.symbol,
        "name": row.name,
        "price_source": row.source,
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "revenue_caliber": {
            "declared": payload.revenue_basis,
            "annualization_factor_applied": factor,
            "revenue_used": base.revenue,
        },
        "applicability": model_applicability(profile_for(row.industry)[0].key),
        "disclaimer": DISCLAIMER,
    }


class PortfolioContextRequest(BaseModel):
    max_loss_per_trade: float | None = None
    max_symbol_weight: float | None = None
    industry_weight: float | None = None
    max_industry_weight: float | None = None
    horizon: str = ""
    liquidity_needed_soon: bool = False


class AnalysisRequest(BaseModel):
    valuation: ValuationRequest | None = None
    portfolio: PortfolioContextRequest | None = None
    portfolio_notes: list[str] = Field(default_factory=list)
    #: 允许把"现价对应的短线视角"显式排除，避免与长期论点混用
    exclude_short_term_rules: bool = True


@router.post("/{symbol}/analysis")
def post_analysis(
    symbol: str, payload: AnalysisRequest, db: Session = Depends(get_db)
) -> dict:
    """统一 7 项输出。缺假设/缺约束时如实输出「暂不行动」并列出缺什么。"""
    row = _require_snapshot(db, symbol)
    statement = _statement_detail_from_row(row)
    statement_payload = (
        statement.to_dict(revenue=row.revenue, net_profit=row.net_profit_parent)
        if statement is not None else None
    )
    as_of = beijing_today()
    quality = assess_quality(_quality_inputs(row, as_of))
    confidence = evidence_confidence(
        coverage=quality.coverage,
        report_date=row.report_date,
        as_of=as_of,
        industry_identified=bool(row.industry),
        has_statement_detail=_statement_detail_from_row(row) is not None,
        sources=2 if _statement_detail_from_row(row) is not None else 1,
    )
    band = None
    basis = ""
    if payload.valuation is not None:
        try:
            basis = payload.valuation.basis
            factor = annualization_factor(row.report_date) or 1.0
            band = scenario_band(
                payload.valuation.to_assumptions(annualization=factor),
                bear_overrides=payload.valuation.bear_overrides,
                bull_overrides=payload.valuation.bull_overrides,
            )
        except ValuationError as exc:
            raise HTTPException(status_code=422, detail=f"假设不成立：{exc}") from exc
        if band.base is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "基准情景假设不成立，无法输出情景与价值判断："
                    f"{band.errors.get('基准', '未知原因')}"
                ),
            )

    portfolio = None
    if payload.portfolio is not None:
        portfolio = PortfolioContext(
            max_loss_per_trade=payload.portfolio.max_loss_per_trade,
            max_symbol_weight=payload.portfolio.max_symbol_weight,
            industry_weight=payload.portfolio.industry_weight,
            max_industry_weight=payload.portfolio.max_industry_weight,
            horizon=payload.portfolio.horizon,
            liquidity_needed_soon=payload.portfolio.liquidity_needed_soon,
        )
    return build_analysis(
        DecisionInputs(
            symbol=row.symbol,
            name=row.name,
            quality=quality,
            confidence=confidence,
            price=row.price,
            report_date=row.report_date,
            fetched_at=row.fetched_at.isoformat() if row.fetched_at else None,
            industry=row.industry,
            source=row.source,
            valuation=band,
            valuation_basis=basis,
            portfolio=portfolio,
            portfolio_notes=tuple(payload.portfolio_notes),
            statement_detail=statement_payload,
        )
    )


class ExplainRequest(AnalysisRequest):
    """在统一分析之上，请求一段**有引用的解释**（数字仍全部来自确定性计算）。"""

    question: str = Field(default="", max_length=500)


def _local_harness_config() -> ExplainerConfig | None:
    """读取 DeepSeek Harness 本机回环桥配置，不回显或复制令牌。"""
    path = Path.home() / ".dsh-tools" / "dsh-api" / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = str(payload.get("apiToken") or "")
        port = int(payload.get("port") or 0)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not token or not 1 <= port <= 65535:
        return None
    return ExplainerConfig(
        enabled=True,
        api_key=token,
        model="deepseek-flash",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout_seconds=60.0,
        # DeepSeek V4 会把推理 token 也计入 max_tokens；1200 在复杂证据包上可能
        # 推理耗尽后正文为空。留足额度，正文仍由引用门禁约束。
        max_output_tokens=4096,
    )


def _resolve_explainer() -> tuple[DeepSeekExplainer, str]:
    settings = get_settings()
    explicit = ExplainerConfig(
            enabled=settings.explain_enabled,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.deepseek_timeout_seconds,
            max_output_tokens=settings.deepseek_max_output_tokens,
        )
    if explicit.enabled and explicit.api_key and explicit.model:
        return DeepSeekExplainer(explicit), "explicit_api"
    if settings.explain_local_harness_enabled:
        local = _local_harness_config()
        if local is not None:
            return DeepSeekExplainer(local), "local_deepseek_harness"
    return DeepSeekExplainer(explicit), "not_configured"


def _explainer() -> DeepSeekExplainer:
    return _resolve_explainer()[0]


@router.get("/explain/status", tags=["fundamentals"])
def explain_status() -> dict:
    """解释层是否就绪（不回显完整密钥；未配置时说明缺哪一项）。"""
    explainer, source = _resolve_explainer()
    return {
        **explainer.status(),
        "connection_source": source,
        "note": (
            "优先使用显式 API 配置；启用 EXPLAIN_LOCAL_HARNESS_ENABLED 后可复用本机"
            " DeepSeek Harness 回环接口，令牌仅在运行时读取，不写入项目或数据库"
        ),
    }


@router.post("/{symbol}/explain")
async def post_explain(
    symbol: str, payload: ExplainRequest, db: Session = Depends(get_db)
) -> dict:
    """返回「确定性分析 + 解释」。解释失败/未配置/引用不合格都如实标注，不影响分析结果。"""
    analysis = post_analysis(symbol, payload, db)
    pack = build_evidence_pack(analysis, generated_at=now_cst().isoformat())
    explanation = await _explainer().explain(pack, question=payload.question or None)
    return {
        "analysis": analysis,
        "evidence_pack": pack.to_payload(),
        "explanation": explanation,
        "boundary": (
            "解释层只能引用证据包里的数字；不参与计算、不写库、不影响策略门禁，也没有下单权限"
        ),
    }
