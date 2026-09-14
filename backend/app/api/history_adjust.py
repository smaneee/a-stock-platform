"""前复权日线回填 API。

回填是全市场动作：拿通达信除权除息数据（``get_xdxr_info``）配合本地未复权日线，
在本地推导前复权因子，写回 ``historical_bars`` 的 ``adjust='qfq'`` 行。全市场一轮
实测约 4~5 分钟，因此走后台任务 + 状态轮询。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_qfq_backfill_runner
from app.history.qfq_service import QfqBackfillRunner
from app.validation import sanitize_symbols

router = APIRouter(prefix="/api/history-adjust", tags=["history-adjust"])


@router.get(
    "",
    summary="前复权回填状态",
)
async def get_history_adjust(
    runner: QfqBackfillRunner = Depends(get_qfq_backfill_runner),
) -> dict:
    """返回回填任务状态与最近一次汇总（状态在内存里，重启后回到 idle）。"""
    return runner.status()


@router.post(
    "",
    status_code=202,
    summary="启动前复权日线回填",
)
async def start_history_adjust(
    symbols: str | None = Query(
        None, description="逗号分隔的标的代码；缺省为本地全部有未复权日线的标的"
    ),
    runner: QfqBackfillRunner = Depends(get_qfq_backfill_runner),
) -> dict:
    """启动全市场（或指定标的）前复权回填，立即返回 202 与状态。

    只写 ``historical_bars`` 里 ``adjust='qfq'`` 的行，按标的先删后插、可重复执行；
    不改动不复权数据。拿不到除权除息数据的标的不写库，只会出现在报告的失败列表里
    —— 宁可不写，也不能把不复权数据伪装成前复权。
    """
    targets: list[str] | None = None
    if symbols is not None and symbols.strip():
        targets = sanitize_symbols(symbols.split(","))
        if not targets:
            raise HTTPException(status_code=422, detail="symbols 里没有合法的 A 股代码")
    return await runner.start(targets)