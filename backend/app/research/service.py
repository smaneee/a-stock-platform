"""研究复核服务：当前状态重算、单条复核、**自动扫描并落库提醒**。

为什么独立成 service：复核逻辑原先写在 API 层，自动扫描（定时任务）也要用同一套
重算与触发判断，放在 API 里会形成 ``tasks → api`` 的反向依赖。这里统一收口：

* :func:`recompute_current` —— 用冻结时的同一套假设重算"当前状态"（保证差异只来自数据变化）
* :func:`review_run` —— 单条记录的复核结果（API 与前端用）
* :func:`scan_reminders` —— 扫描**每个标的最新一条记录**，把触发的条件写进
  ``research_review_reminders``（幂等：同一天同一记录同一条件只写一条）
* :func:`list_reminders` / :func:`acknowledge_reminder` —— 待办列表与"已查看"

纪律：扫描只写自己的提醒表，**不改动任何研究记录**；缺快照的标的记为 ``skipped``
并在结果里说明原因，不伪造"无触发"。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import InvestmentResearchRun, ResearchReviewReminder
from app.fundamentals.repository import latest_snapshot
from app.market_rules.session_state import now_cst
from app.research.review import build_review
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

#: 严重度排序（high 先）
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _valuation_request(assumptions: dict):
    """把冻结的假设还原成估值请求体（字段一一对应，不做任何补全）。"""
    from app.api.fundamentals import ValuationRequest

    valuation = (assumptions or {}).get("valuation") or {}
    return ValuationRequest(**valuation) if valuation else None


def _run_detail(row: InvestmentResearchRun) -> dict:
    """与 API 层 ``_detail`` 结构一致的冻结记录（复核比较需要）。"""
    return {
        "id": row.id,
        "symbol": row.symbol,
        "name": row.name,
        "snapshot_date": row.snapshot_date.isoformat(),
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "price": row.price,
        "source": row.source,
        "conclusion_key": row.conclusion_key,
        "conclusion": (row.analysis or {}).get("1_conclusion", {}).get("conclusion"),
        "explanation_status": row.explanation_status,
        "fingerprint": row.fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "assumptions": row.assumptions,
        "analysis": row.analysis,
        "reverse_valuation": row.reverse_valuation,
        "explanation": row.explanation,
    }


def recompute_current(db: Session, row: InvestmentResearchRun) -> tuple[dict, dict] | None:
    """重算当前状态：返回（快照元数据, 统一分析）。缺快照或假设无法还原时返回 ``None``。

    为什么要容错：冻结记录里的估值假设是历史数据，可能出现**字段不全**（早期版本、
    人工补录、迁移残留）。定时扫描是无人值守任务，不能因为一条历史记录假设不完整
    就整轮崩掉 —— 这里返回 ``None``，由上层如实报告"无法复核"并记原因。
    """
    from app.api.fundamentals import AnalysisRequest, PortfolioContextRequest, post_analysis
    from pydantic import ValidationError

    snapshot = latest_snapshot(db, row.symbol)
    if snapshot is None:
        return None
    meta = {
        "symbol": snapshot.symbol,
        "name": snapshot.name,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "report_date": snapshot.report_date.isoformat() if snapshot.report_date else None,
        "price": snapshot.price,
        "source": snapshot.source,
    }
    assumptions = row.assumptions or {}
    try:
        valuation = _valuation_request(assumptions)
    except ValidationError as exc:
        logger.warning("研究记录 #%s 的冻结假设无法还原：%s", row.id, exc.error_count())
        return None
    request = AnalysisRequest(
        valuation=valuation,
        portfolio=PortfolioContextRequest(horizon=assumptions.get("horizon") or ""),
    )
    return meta, post_analysis(row.symbol, request, db)


def previous_run(db: Session, row: InvestmentResearchRun) -> InvestmentResearchRun | None:
    """同一标的上一条记录（按 id 倒序）。"""
    return (
        db.query(InvestmentResearchRun)
        .filter(
            InvestmentResearchRun.symbol == row.symbol,
            InvestmentResearchRun.id < row.id,
        )
        .order_by(InvestmentResearchRun.id.desc())
        .first()
    )


def review_run(db: Session, row: InvestmentResearchRun, *, today: date | None = None) -> dict:
    """单条记录的复核结果（不需要写库）。"""
    current = recompute_current(db, row)
    if current is None:
        return {
            "needs_review": None,
            "fired_count": 0,
            "fired_triggers": [],
            "all_triggers": [],
            "has_previous_run": False,
            "diff": None,
            "note": (
                f"库里没有 {row.symbol} 的当前基本面快照，无法重算当前状态 → "
                "不给出复核结论（缺数据就是缺数据），请先刷新快照"
            ),
        }
    meta, analysis = current
    previous = previous_run(db, row)
    return build_review(
        _run_detail(row),
        previous=_run_detail(previous) if previous is not None else None,
        current_snapshot=meta,
        current_analysis=analysis,
        today=today or now_cst().date(),
    )


def latest_runs_by_symbol(db: Session, limit: int = 200) -> list[InvestmentResearchRun]:
    """每个标的最新一条研究记录（按记录时间倒序）。"""
    rows = (
        db.query(InvestmentResearchRun)
        .order_by(InvestmentResearchRun.created_at.desc(), InvestmentResearchRun.id.desc())
        .limit(limit)
        .all()
    )
    seen: set[str] = set()
    latest: list[InvestmentResearchRun] = []
    for row in rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        latest.append(row)
    return latest


def scan_reminders(db: Session, *, today: date | None = None, limit: int = 200) -> dict:
    """扫描每个标的最新记录，把触发的条件写入提醒表（幂等）。"""
    day = today or now_cst().date()
    created = 0
    updated = 0
    skipped: list[dict] = []
    scanned: list[dict] = []
    for row in latest_runs_by_symbol(db, limit=limit):
        result = review_run(db, row, today=day)
        if result["needs_review"] is None:
            skipped.append({"symbol": row.symbol, "run_id": row.id, "reason": result["note"]})
            continue
        scanned.append({
            "symbol": row.symbol,
            "run_id": row.id,
            "needs_review": result["needs_review"],
            "fired": result["fired_count"],
        })
        for trigger in result["fired_triggers"]:
            # S4 验收「重复事件不重复提醒」：同一个未确认(未处理)的条件再被检出时，
            # 只更新文案，**不再新增一条** —— 否则同一个悬而未决的问题会每天堆一条。
            # 已确认过的条件若再次触发，则视为"重新出现的同一问题"，允许新开一条，
            # 这样复核记录里能看出"处理过、又发生了"。
            open_row = db.execute(
                select(ResearchReviewReminder).where(
                    ResearchReviewReminder.run_id == row.id,
                    ResearchReviewReminder.trigger_name == trigger["name"],
                    ResearchReviewReminder.acknowledged.is_(False),
                )
            ).scalars().first()
            if open_row is not None:
                open_row.detail = trigger["detail"][:255]
                open_row.label = trigger["label"][:120]
                open_row.severity = trigger["severity"]
                updated += 1
                continue
            exists = db.execute(
                select(ResearchReviewReminder).where(
                    ResearchReviewReminder.run_id == row.id,
                    ResearchReviewReminder.trigger_name == trigger["name"],
                    ResearchReviewReminder.detected_on == day,
                )
            ).scalar_one_or_none()
            if exists is not None:
                # 同一天、已确认过的同条件重复扫描：只更新文案，不重复计数
                exists.detail = trigger["detail"][:255]
                exists.label = trigger["label"][:120]
                continue
            db.add(
                ResearchReviewReminder(
                    run_id=row.id,
                    symbol=row.symbol,
                    trigger_name=trigger["name"],
                    label=trigger["label"][:120],
                    detail=trigger["detail"][:255],
                    severity=trigger["severity"],
                    detected_on=day,
                    created_at=utc_now(),
                )
            )
            created += 1
    db.commit()
    return {
        "detected_on": day.isoformat(),
        "symbols_scanned": len(scanned),
        "reminders_created": created,
        "reminders_updated": updated,
        "skipped": skipped,
        "details": scanned,
    }


def list_reminders(
    db: Session, *, include_acknowledged: bool = False, limit: int = 100
) -> dict:
    """待办列表；默认只看未确认的，按严重度与时间倒序。"""
    query = db.query(ResearchReviewReminder)
    if not include_acknowledged:
        query = query.filter(ResearchReviewReminder.acknowledged.is_(False))
    rows = query.order_by(
        ResearchReviewReminder.detected_on.desc(), ResearchReviewReminder.id.desc()
    ).limit(limit).all()
    items = [
        {
            "id": row.id,
            "run_id": row.run_id,
            "symbol": row.symbol,
            "trigger_name": row.trigger_name,
            "label": row.label,
            "detail": row.detail,
            "severity": row.severity,
            "detected_on": row.detected_on.isoformat(),
            "acknowledged": row.acknowledged,
            "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        }
        for row in rows
    ]
    items.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 3), item["id"]))
    unacknowledged = sum(1 for item in items if not item["acknowledged"])
    return {
        "count": len(items),
        "unacknowledged_count": unacknowledged,
        "items": items,
        "note": "提醒只说明「该重新看一遍」，不代表观点对错，也不构成投资建议。",
    }


def acknowledge_reminder(db: Session, reminder_id: int) -> dict:
    row = db.get(ResearchReviewReminder, reminder_id)
    if row is None:
        return {"ok": False, "reason": f"未找到提醒：{reminder_id}"}
    if not row.acknowledged:
        row.acknowledged = True
        row.acknowledged_at = utc_now()
        db.commit()
    return {"ok": True, "id": row.id, "acknowledged": True}
