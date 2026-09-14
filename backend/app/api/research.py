"""实验预登记接口（P0-05 的 D10）。

* ``GET  /api/research/preregistrations``            列出全部预登记 + 冻结哈希校验
* ``GET  /api/research/preregistrations/{id}``       单条详情
* ``POST /api/research/preregistrations/{id}/unblind`` 一次性揭盲（第二次 409）
* ``POST /api/research/preregistrations/adjust``     多重比较校正

预登记产物放在 ``docs/evidence/preregistrations/<id>.json``（随代码进版本控制）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import PROJECT_DIR
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
