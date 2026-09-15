"""实验预登记接口（P0-05 的 D10）。

* ``GET  /api/research/preregistrations``            列出全部预登记 + 冻结哈希校验
* ``GET  /api/research/preregistrations/{id}``       单条详情
* ``POST /api/research/preregistrations/{id}/unblind`` 一次性揭盲（第二次 409）
* ``POST /api/research/preregistrations/adjust``     多重比较校正

预登记产物放在 ``docs/evidence/preregistrations/<id>.json``（随代码进版本控制）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_DIR
from app.api.fundamentals import (
    AnalysisRequest,
    PortfolioContextRequest,
    ReverseValuationRequest,
    ValuationRequest,
    _explainer,
    _require_snapshot,
    post_analysis,
    post_reverse_valuation,
)
from app.database.models import HistoricalBar, InvestmentResearchRun, PaperAccount, PaperPosition
from app.database.session import get_db
from app.explain.deepseek import build_evidence_pack
from app.fundamentals.repository import latest_snapshot
from app.market_rules.session_state import now_cst
from app.research.preregistration import (
    MULTIPLE_COMPARISON_METHODS,
    Preregistration,
    adjust_pvalues,
    summarize_correction,
)
from app.research.review import build_review
from app.research.thesis import (
    CRITIC_PROMPT,
    build_thesis_card,
    card_from_payload,
    position_gate,
)
from app.research.service import (
    acknowledge_reminder,
    list_reminders,
    scan_reminders,
)
from app.research.rebalance import (
    ResearchDraftConstraints,
    ResearchDraftInputs,
    aligned_return_correlation,
    build_research_draft,
)
from app.paper_trading.portfolio import PortfolioService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/research", tags=["research"])

PREREGISTRATION_DIR = PROJECT_DIR / "docs" / "evidence" / "preregistrations"


class AdjustRequest(BaseModel):
    pvalues: list[float] = Field(..., min_length=1, description="各次试验的原始 p 值")
    method: str = Field(
        "benjamini_hochberg",
        description=f"多重比较方法：{' / '.join(MULTIPLE_COMPARISON_METHODS)}",
    )
    alpha: float = Field(0.05, gt=0, lt=1)


def _record_path(experiment_id: str) -> Path:
    safe = "".join(ch for ch in experiment_id if ch.isalnum() or ch in "-_")
    if safe != experiment_id or not safe:
        raise HTTPException(status_code=422, detail="experiment_id 只允许字母/数字/下划线/短横线")
    return PREREGISTRATION_DIR / f"{safe}.json"


@router.get("/preregistrations")
def list_preregistrations() -> dict:
    """列出全部预登记；哈希不一致的条目会被标记为 integrity_ok=false（而不是隐藏）。"""
    items: list[dict] = []
    problems: list[str] = []
    if PREREGISTRATION_DIR.is_dir():
        for path in sorted(PREREGISTRATION_DIR.glob("*.json")):
            try:
                items.append(Preregistration.load(path).to_dict())
            except ValueError as exc:
                problems.append(f"{path.name}: {exc}")
    return {
        "count": len(items),
        "items": items,
        "integrity_problems": problems,
        "directory": str(PREREGISTRATION_DIR),
    }


@router.get("/preregistrations/{experiment_id}")
def get_preregistration(experiment_id: str) -> dict:
    path = _record_path(experiment_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未找到预登记：{experiment_id}")
    try:
        return Preregistration.load(path).to_dict()
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"error": "preregistration_tampered", "message": str(exc)}
        ) from exc


@router.post("/preregistrations/{experiment_id}/unblind")
def unblind_preregistration(experiment_id: str, note: str = "") -> dict:
    """一次性揭盲。第二次调用返回 409 并说明首次揭盲时间。"""
    path = _record_path(experiment_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未找到预登记：{experiment_id}")
    try:
        record = Preregistration.load(path)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"error": "preregistration_tampered", "message": str(exc)}
        ) from exc

    result = record.unblind(note=note)
    if result["allowed"]:
        record.save(path)
        logger.warning("实验 %s 首次揭盲（留出段只允许一次）", experiment_id)
        return result
    raise HTTPException(status_code=409, detail={"error": "already_unblinded", **result})


@router.post("/adjust")
def adjust(body: AdjustRequest) -> dict:
    """多重比较校正：把「试了多少次」计入显著性判定。"""
    try:
        result = adjust_pvalues(body.pvalues, body.method, body.alpha)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["summary"] = summarize_correction(result)
    return result


class CreateInvestmentResearchRunRequest(BaseModel):
    """一次研究冻结所需的全部显式输入。"""

    valuation: ValuationRequest
    horizon: str = Field(default="", max_length=40)
    question: str = Field(
        default="请站在反方投资委员立场，指出最关键的反对证据、尚未验证事项和论点失效条件。只引用证据包已有数字和 id。",
        max_length=500,
    )
    include_explanation: bool = True


class ResearchRebalanceDraftRequest(BaseModel):
    """用户显式给出的组合约束；服务端不猜风险偏好。"""

    account_id: int
    max_symbol_weight: float = Field(0.10, gt=0, le=0.20)
    max_industry_weight: float = Field(0.30, gt=0, le=0.50)
    max_loss_per_trade: float = Field(0.01, gt=0, le=0.05)
    stop_distance: float = Field(0.10, ge=0.03, le=0.30)
    max_portfolio_drawdown: float = Field(0.12, ge=0.05, le=0.30)
    max_correlation: float = Field(0.75, ge=0, le=0.95)
    max_liquidity_participation: float = Field(0.01, gt=0, le=0.10)
    correlation_lookback_days: int = Field(60, ge=20, le=250)


def _summary(row: InvestmentResearchRun) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "name": row.name,
        "snapshot_date": row.snapshot_date.isoformat(),
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "price": row.price,
        "source": row.source,
        "conclusion_key": row.conclusion_key,
        "conclusion": row.analysis.get("1_conclusion", {}).get("conclusion"),
        "explanation_status": row.explanation_status,
        "fingerprint": row.fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _detail(row: InvestmentResearchRun) -> dict:
    return {
        **_summary(row),
        "assumptions": row.assumptions,
        "analysis": row.analysis,
        "reverse_valuation": row.reverse_valuation,
        "explanation": row.explanation,
        "thesis_card": row.thesis_card,
        "model_total_tokens": row.model_total_tokens,
        "position_gate": (
            position_gate(card_from_payload(row.thesis_card))
            if row.thesis_card
            else None
        ),
        "immutability_note": "研究记录创建后不提供修改接口；新的观点应创建新记录并比较指纹。",
    }


def _commit_with_lock_retry(db: Session, *, attempts: int = 3, delay: float = 0.5) -> None:
    """提交研究记录，对 SQLite 写锁做**有限重试**。

    为什么需要（2026-09-15 实测）：创建研究记录时遇到
    ``sqlite3.OperationalError: database is locked`` → 接口直接 500。
    原因是后台 worker（组合回测轮询等）与请求写入争用同一把写锁。研究记录**是不可变的
    审计记录**，一次瞬时锁冲突就丢掉整次重算（含数十秒的模型调用）代价太高，
    因此对"锁"这一类瞬时错误做有限重试；其他错误照旧抛出，不掩盖真实缺陷。
    """
    for attempt in range(1, attempts + 1):
        try:
            db.commit()
            return
        except OperationalError as exc:
            db.rollback()
            locked = "locked" in str(exc).lower()
            if not locked or attempt == attempts:
                logger.warning("研究记录提交失败（第 %s 次）：%s", attempt, exc)
                raise
            logger.warning("研究记录提交遇到写锁，%s 秒后重试（第 %s 次）", delay * attempt, attempt)
            time.sleep(delay * attempt)


def _merge_dual_review(primary: dict, critic: dict) -> dict:
    """合并两次独立审查；任一轮失败都不能伪装成完整通过。"""
    statuses = [str(primary.get("status") or "error"), str(critic.get("status") or "error")]
    if "rejected" in statuses:
        status = "rejected"
    elif statuses == ["ok", "ok"]:
        status = "ok"
    elif "not_configured" in statuses:
        status = "not_configured"
    else:
        status = "error"
    primary_text = str(primary.get("text") or "").strip()
    critic_text = str(critic.get("text") or "").strip()
    text = ""
    if primary_text:
        text += "【主审意见】\n" + primary_text
    if critic_text:
        text += ("\n\n" if text else "") + "【独立反方意见】\n" + critic_text
    return {
        "status": status,
        "mode": "independent_dual_review",
        "text": text,
        "primary": primary,
        "critic": critic,
        "validation": {
            "passed": statuses == ["ok", "ok"],
            "primary_passed": (primary.get("validation") or {}).get("passed", False),
            "critic_passed": (critic.get("validation") or {}).get("passed", False),
        },
        "note": "主审与独立反方分别生成并分别通过数字引用门禁；任一轮失败时整体不标记为通过。",
    }


@router.post("/runs")
async def create_investment_research_run(
    body: CreateInvestmentResearchRunRequest,
    symbol: str = Query(..., min_length=6, max_length=10),
    db: Session = Depends(get_db),
) -> dict:
    """由服务端重算并冻结研究，防止只保存前端展示文字。"""
    snapshot = _require_snapshot(db, symbol)
    analysis_request = AnalysisRequest(
        valuation=body.valuation,
        portfolio=PortfolioContextRequest(horizon=body.horizon),
    )
    analysis = post_analysis(symbol, analysis_request, db)
    reverse_request = ReverseValuationRequest(
        revenue=body.valuation.revenue,
        fcf_margin=body.valuation.fcf_margin,
        discount_rate=body.valuation.discount_rate,
        terminal_growth=body.valuation.terminal_growth,
        shares=body.valuation.shares,
        net_debt=body.valuation.net_debt,
        years=body.valuation.years,
        basis=body.valuation.basis,
        revenue_basis=body.valuation.revenue_basis,
    )
    reverse = post_reverse_valuation(symbol, reverse_request, db)

    # 解释层可能等待数十秒。先复制快照元数据并结束只读事务，避免大型 SQLite
    # 数据库在模型等待期间持有共享锁，阻塞后台任务或本次记录写入。
    snapshot_meta = {
        "symbol": snapshot.symbol,
        "name": snapshot.name,
        "snapshot_date": snapshot.snapshot_date,
        "report_date": snapshot.report_date,
        "price": snapshot.price,
        "source": snapshot.source,
    }
    db.rollback()

    explanation: dict | None = None
    explanation_status = "not_requested"
    if body.include_explanation:
        pack = build_evidence_pack(analysis, generated_at=now_cst().isoformat())
        explainer = _explainer()
        primary = await explainer.explain(pack, question=body.question or None)
        critic = await explainer.explain(pack, question=CRITIC_PROMPT)
        explanation = _merge_dual_review(primary, critic)
        explanation_status = str(explanation.get("status") or "unknown")

    assumptions = {
        "valuation": body.valuation.model_dump(),
        "horizon": body.horizon,
        "question": body.question,
        "include_explanation": body.include_explanation,
    }
    # S1：结构化决策卡（骨架由代码生成；模型文字经证据 id 与数字双重校验）
    pack = build_evidence_pack(analysis, generated_at=now_cst().isoformat())
    primary_text = ""
    variant_text = ""
    if explanation and explanation.get("status") == "ok":
        primary_text = str((explanation.get("primary") or {}).get("text") or "")
        variant_text = str((explanation.get("critic") or {}).get("text") or "")
    thesis_card = build_thesis_card(
        analysis,
        pack,
        horizon=body.horizon or None,
        model_text=primary_text,
        variant_text=variant_text,
        model_usage={
            "latency_ms": None,
            "total_tokens": ((explanation or {}).get("usage") or {}).get("total_tokens"),
            "note": "只记录 token 用量；费率未经核实，不做金额估算",
        },
        fetched_at=now_cst().isoformat(),
        text_origin="model_primary_and_critic" if primary_text else "deterministic_skeleton",
    )
    gate = position_gate(thesis_card)

    frozen = {
        "assumptions": assumptions,
        "analysis": analysis,
        "reverse_valuation": reverse,
        "explanation": explanation,
    }
    fingerprint = hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    row = InvestmentResearchRun(
        **snapshot_meta,
        conclusion_key=str(analysis["1_conclusion"]["conclusion_key"]),
        explanation_status=explanation_status,
        fingerprint=fingerprint,
        assumptions=assumptions,
        analysis=analysis,
        reverse_valuation=reverse,
        explanation=explanation,
        thesis_card=thesis_card.model_dump(mode="json"),
        model_total_tokens=((explanation or {}).get("usage") or {}).get("total_tokens"),
    )
    db.add(row)
    _commit_with_lock_retry(db)
    db.refresh(row)
    return _detail(row)


@router.get("/runs")
def list_investment_research_runs(
    symbol: str | None = Query(default=None, min_length=6, max_length=10),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(InvestmentResearchRun)
    if symbol:
        query = query.filter(InvestmentResearchRun.symbol == symbol)
    rows = query.order_by(InvestmentResearchRun.created_at.desc(), InvestmentResearchRun.id.desc()).limit(limit).all()
    return {"count": len(rows), "items": [_summary(row) for row in rows]}


@router.get("/runs/{run_id}")
def get_investment_research_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(InvestmentResearchRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到研究记录：{run_id}")
    return _detail(row)


def _daily_closes(db: Session, symbol: str, limit: int) -> dict[str, float]:
    rows = db.execute(
        select(HistoricalBar.trade_date, HistoricalBar.close)
        .where(
            HistoricalBar.symbol == symbol,
            HistoricalBar.period == "daily",
            HistoricalBar.adjust == "qfq",
        )
        .order_by(HistoricalBar.trade_date.desc())
        .limit(limit + 1)
    ).all()
    return {day.isoformat(): float(close) for day, close in rows if float(close) > 0}


def _average_amount_20(db: Session, symbol: str) -> float | None:
    rows = db.scalars(
        select(HistoricalBar.amount)
        .where(
            HistoricalBar.symbol == symbol,
            HistoricalBar.period == "daily",
            HistoricalBar.adjust == "qfq",
            HistoricalBar.amount > 0,
        )
        .order_by(HistoricalBar.trade_date.desc())
        .limit(20)
    ).all()
    values = [float(value) for value in rows if float(value) > 0]
    return sum(values) / len(values) if len(values) >= 10 else None


@router.post("/runs/{run_id}/rebalance-draft")
async def create_research_rebalance_draft(
    run_id: int,
    body: ResearchRebalanceDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """把一条冻结研究映射为只读模拟调仓草案，不写订单、不自动执行。"""
    run = db.get(InvestmentResearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未找到研究记录：{run_id}")
    account = db.get(PaperAccount, body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"未找到模拟账户：{body.account_id}")

    positions = db.scalars(
        select(PaperPosition).where(PaperPosition.account_id == body.account_id)
    ).all()
    quantities: dict[str, int] = {}
    for position in positions:
        quantities[position.symbol] = quantities.get(position.symbol, 0) + position.quantity
    symbols = sorted(set(quantities) | {run.symbol})
    quotes = await request.app.state.provider_manager.get_quotes(symbols)
    missing_quotes = [
        symbol
        for symbol in symbols
        if symbol not in quotes or quotes[symbol].is_stale or quotes[symbol].price <= 0
    ]
    if missing_quotes:
        raise HTTPException(
            status_code=422,
            detail=f"缺少有效实时行情：{', '.join(missing_quotes[:5])}",
        )

    snapshot = PortfolioService(db).calculate_snapshot(account, quotes)
    if snapshot.total_asset <= 0:
        raise HTTPException(status_code=422, detail="模拟账户总资产必须大于 0")
    current_quantity = quantities.get(run.symbol, 0)
    current_value = current_quantity * quotes[run.symbol].price
    current_weight = current_value / snapshot.total_asset
    invested_weight = sum(snapshot.current_position_value.values()) / snapshot.total_asset

    industry_by_symbol: dict[str, str | None] = {}
    for symbol in quantities:
        item = latest_snapshot(db, symbol)
        industry_by_symbol[symbol] = item.industry if item is not None else None
    target_snapshot = latest_snapshot(db, run.symbol)
    target_industry = target_snapshot.industry if target_snapshot is not None else None
    industry_weight = None
    if target_industry and all(industry_by_symbol.get(symbol) for symbol in quantities):
        industry_value = sum(
            snapshot.current_position_value.get(symbol, 0.0)
            for symbol in quantities
            if industry_by_symbol.get(symbol) == target_industry
        )
        industry_weight = industry_value / snapshot.total_asset

    peers = [symbol for symbol, quantity in quantities.items() if symbol != run.symbol and quantity > 0]
    target_closes = _daily_closes(db, run.symbol, body.correlation_lookback_days)
    correlations: list[tuple[str, float, int]] = []
    for symbol in peers:
        value, observations = aligned_return_correlation(
            target_closes,
            _daily_closes(db, symbol, body.correlation_lookback_days),
        )
        if value is not None:
            correlations.append((symbol, value, observations))
    # 正相关才会叠加同向暴露；负相关具有分散作用，也不能掩盖另一个较高的正相关持仓。
    strongest = max(correlations, key=lambda item: item[1], default=None)

    draft = build_research_draft(
        ResearchDraftInputs(
            conclusion_key=run.conclusion_key,
            symbol=run.symbol,
            price=quotes[run.symbol].price,
            total_asset=snapshot.total_asset,
            current_quantity=current_quantity,
            current_weight=current_weight,
            industry=target_industry,
            industry_weight=industry_weight,
            invested_weight=invested_weight,
            average_amount_20=_average_amount_20(db, run.symbol),
            max_correlation=strongest[1] if strongest else None,
            correlation_observations=strongest[2] if strongest else 0,
            held_peer_count=len(peers),
        ),
        ResearchDraftConstraints(
            max_symbol_weight=body.max_symbol_weight,
            max_industry_weight=body.max_industry_weight,
            max_loss_per_trade=body.max_loss_per_trade,
            stop_distance=body.stop_distance,
            max_portfolio_drawdown=body.max_portfolio_drawdown,
            max_correlation=body.max_correlation,
            max_liquidity_participation=body.max_liquidity_participation,
        ),
    )
    # S1/S4：用**冻结决策卡**的增仓门禁再挡一道 —— 关键数据缺失、引用无效、方法不适用时，
    # 不允许生成任何新增订单（草案仍然展示风险检查，但 proposed_order 强制为空）。
    # 历史记录若没有结构化决策卡，同样不放行：缺结构就是缺依据。
    frozen_card = card_from_payload(run.thesis_card) if run.thesis_card else None
    thesis_gate = (
        position_gate(frozen_card, current_weight=round(current_weight, 6))
        if frozen_card is not None
        else {
            "allowed": False,
            "blockers": ["该研究记录没有冻结的结构化决策卡，不能作为增仓依据"],
            "note": "历史记录缺少结构化论点；请用新版流程创建研究记录后再生成草案",
            "current_weight": round(current_weight, 6),
        }
    )
    if not thesis_gate["allowed"]:
        draft = {**draft, "proposed_order": None}

    return {
        "research_run": _summary(run),
        "account": {
            "id": account.id,
            "name": account.name,
            "total_asset": snapshot.total_asset,
            "invested_weight": round(invested_weight, 6),
        },
        "correlation": {
            "strongest_symbol": strongest[0] if strongest else None,
            "strongest_value": strongest[1] if strongest else None,
            "observations": strongest[2] if strongest else 0,
            "held_peer_count": len(peers),
        },
        "constraints": body.model_dump(),
        **draft,
        "thesis_gate": thesis_gate,
        "disclaimer": "调仓草案只用于研究与模拟，不构成投资建议，也不会自动提交任何订单。",
    }


@router.get("/runs/{run_id}/review")
def review_investment_research_run(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """复核某条研究记录：**是否需要重新研究**、触发了哪些条件、与上次记录有哪些差异。

    复核不做任何写操作：当前值由服务端**重新计算**（同一套确定性代码），
    只读快照表与冻结记录；没有记录、快照缺失时如实说明，不猜。
    """
    row = db.get(InvestmentResearchRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到研究记录：{run_id}")

    snapshot = latest_snapshot(db, row.symbol)
    if snapshot is None:
        return {
            "run": _summary(row),
            "needs_review": None,
            "note": (
                f"库里没有 {row.symbol} 的当前基本面快照，无法重算当前状态 → "
                "不给出复核结论（缺数据就是缺数据），请先刷新快照"
            ),
        }

    today = now_cst().date()
    current_snapshot = {
        "symbol": snapshot.symbol,
        "name": snapshot.name,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "report_date": snapshot.report_date.isoformat() if snapshot.report_date else None,
        "price": snapshot.price,
        "source": snapshot.source,
    }
    # 用**冻结时保存的同一套假设**重算当前分析：这样差异只来自数据变化，而不是假设变化
    frozen_assumptions = row.assumptions or {}
    valuation_body = frozen_assumptions.get("valuation") or {}
    analysis_request = AnalysisRequest(
        valuation=ValuationRequest(**valuation_body) if valuation_body else None,
        portfolio=PortfolioContextRequest(horizon=frozen_assumptions.get("horizon") or ""),
    )
    current_analysis = post_analysis(row.symbol, analysis_request, db)
    previous = (
        db.query(InvestmentResearchRun)
        .filter(
            InvestmentResearchRun.symbol == row.symbol,
            InvestmentResearchRun.id < row.id,
        )
        .order_by(InvestmentResearchRun.id.desc())
        .first()
    )
    review = build_review(
        _detail(row),
        previous=_detail(previous) if previous is not None else None,
        current_snapshot=current_snapshot,
        current_analysis=current_analysis,
        today=today,
    )
    return {
        "run": _summary(row),
        "previous_run": _summary(previous) if previous is not None else None,
        "current": {
            "snapshot": current_snapshot,
            "conclusion_key": (current_analysis.get("1_conclusion") or {}).get("conclusion_key"),
        },
        **review,
        "immutability_note": "复核只读；要记录新判断请创建新的研究记录（旧的不会被覆盖）",
        "disclaimer": "复核提醒只说明'该重新看一遍'，不构成投资建议。",
    }


# ── 自动复核提醒（阶段 2 剩余部分） ──────────────────────────────────────
#
# 复核本身是只读的；自动化的价值在于"不用人记得去点"。收盘后定时任务扫描每个标的的最新
# 记录，把触发的条件写进 research_review_reminders，形成可确认的待办列表。


@router.get("/reminders")
def list_research_reminders(
    include_acknowledged: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """复核提醒待办列表（默认只看未确认的，按严重度排序）。"""
    return list_reminders(db, include_acknowledged=include_acknowledged, limit=limit)


@router.post("/reminders/scan")
def scan_research_reminders(
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """立即扫描一次：对每个标的的最新研究记录重算并落库提醒（幂等，可重复调用）。"""
    return scan_reminders(db, limit=limit)


@router.post("/reminders/{reminder_id}/acknowledge")
def acknowledge_research_reminder(
    reminder_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """把某条提醒标记为「已查看」：只是从待办里去掉，历史仍然保留。"""
    result = acknowledge_reminder(db, reminder_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason"))
    return result
