"""策略证据接口（P1-01）。

设计要点（对应研发计划 §5.5、§11.8 与首周清单「禁止手工隐藏负结果」）：

* 证据来自**版本化文件** ``docs/evidence/strategy-evidence.json``，而不是代码里的
  常量或内存态：文件随代码一起进版本控制，改动可审计。
* 接口只做「读文件 + 契约校验 + 汇总」，不提供任何过滤掉负结果的参数；
  负结果条目一旦缺失，``backend/tests/test_evidence_contract.py`` 会失败。
* 首页/证据页展示的状态必须来自本接口，不允许前端自己判定「通过」。
* 附带活体信息：买点雷达的样本外验证任务当前状态（内存态）+ 数据新鲜度，
  让「报告版本」与「当前状态」分开显示。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.config import PROJECT_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/evidence", tags=["evidence"])

EVIDENCE_PATH = PROJECT_DIR / "docs" / "evidence" / "strategy-evidence.json"

#: 必须存在的负结果条目：缺失即视为「隐藏负结果」，接口直接报错而不是静默少一项
REQUIRED_NEGATIVE_IDS = ("buy-point-radar", "sentiment-factor")
REQUIRED_ITEM_IDS = (
    "buy-point-radar",
    "sentiment-factor",
    "portfolio-backtest-engine",
    "indicator-suite",
    "universe-point-in-time",
    "eastmoney-datacenter",
    "paper-trading-loop",
)
VALID_STATUSES = {"unverified", "in_progress", "failed_oos", "inconclusive", "passed_oos"}


def load_evidence(path: Path | None = None) -> dict[str, Any]:
    """读取并校验证据文件；任何结构问题都抛 ValueError（由接口转 503）。"""
    target = path or EVIDENCE_PATH
    if not target.exists():
        raise ValueError(f"证据文件不存在：{target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("证据文件缺少 items")
    ids = [item.get("id") for item in items]
    missing = [i for i in REQUIRED_ITEM_IDS if i not in ids]
    if missing:
        raise ValueError(f"证据文件缺少必需条目：{', '.join(missing)}")
    for item in items:
        status = item.get("status")
        if status not in VALID_STATUSES:
            raise ValueError(f"条目 {item.get('id')} 的状态非法：{status!r}")
        if not item.get("evidence_source"):
            raise ValueError(f"条目 {item.get('id')} 没有证据来源，不可审计")
    return payload


def summary_of(payload: dict[str, Any]) -> dict[str, Any]:
    """汇总给首页状态条用的最小信息（不隐藏任何负结果）。"""
    items = payload["items"]
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    negative = [item["id"] for item in items if item["status"] in {"failed_oos", "inconclusive"}]
    return {
        "total": len(items),
        "by_status": by_status,
        "production_ready_count": sum(1 for item in items if item.get("production_ready")),
        "negative_or_uncertain": negative,
        "artifact_updated_at": payload.get("artifact_updated_at"),
        "schema_version": payload.get("schema_version"),
    }


@router.get("")
async def list_evidence(request: Request) -> dict[str, Any]:
    """全部策略/数据证据条目 + 汇总 + 活体状态。"""
    try:
        payload = load_evidence()
    except ValueError as exc:
        logger.error("证据文件不可用：%s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "evidence_unavailable", "message": str(exc)},
        ) from exc

    live: dict[str, Any] = {}
    validation_service = getattr(request.app.state, "radar_validation_service", None)
    if validation_service is not None:
        try:
            state = validation_service.status()
            live["buy_point_radar_validation"] = state
        except Exception as exc:  # noqa: BLE001 - 活体状态不影响证据本身
            live["buy_point_radar_validation"] = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "schema_version": payload.get("schema_version"),
        "artifact_updated_at": payload.get("artifact_updated_at"),
        "disclaimer": payload.get("disclaimer"),
        "status_vocabulary": payload.get("status_vocabulary"),
        "production_gate": payload.get("production_gate"),
        "summary": summary_of(payload),
        "items": payload["items"],
        "live": live,
        "source_file": str(EVIDENCE_PATH),
    }


@router.get("/summary")
async def evidence_summary() -> dict[str, Any]:
    """只取汇总（首页状态条用，避免拉全量条目）。"""
    try:
        payload = load_evidence()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "evidence_unavailable", "message": str(exc)},
        ) from exc
    return {
        "artifact_updated_at": payload.get("artifact_updated_at"),
        "summary": summary_of(payload),
        "production_gate": payload.get("production_gate"),
    }
