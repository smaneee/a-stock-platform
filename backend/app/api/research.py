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
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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
from app.database.models import InvestmentResearchRun
from app.database.session import get_db
from app.explain.deepseek import build_evidence_pack
from app.market_rules.session_state import now_cst
from app.research.preregistration import (
    MULTIPLE_COMPARISON_METHODS,
    Preregistration,
    adjust_pvalues,
    summarize_correction,
)

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
        "immutability_note": "研究记录创建后不提供修改接口；新的观点应创建新记录并比较指纹。",
    }


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
        critic = await explainer.explain(
            pack,
            question=(
                "你是独立反方投资委员。不要迎合既有结论；优先找出会导致永久损失、"
                "估值失真或论点失效的证据，并明确当前证据无法回答什么。"
            ),
        )
        explanation = _merge_dual_review(primary, critic)
        explanation_status = str(explanation.get("status") or "unknown")

    assumptions = {
        "valuation": body.valuation.model_dump(),
        "horizon": body.horizon,
        "question": body.question,
        "include_explanation": body.include_explanation,
    }
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
    )
    db.add(row)
    db.commit()
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
