"""前向模拟观察计时服务（P1-02）。

对应研发计划：**工程冻结后累计前向模拟交易日；重大成交逻辑修改后重新计时；
不得用历史重放代替前向观察，实盘试点不设倒推日期。**

核心规则（全部可测试）：

1. ``start`` 以 ``freeze_tag`` 幂等：同一冻结只登记一次，不覆盖历史；
2. ``count_day`` 只统计**严格晚于** ``started_on``、**不晚于今天（北京时间）**、
   且**是交易日**的日期；同一日期只计一次（重复调用返回 counted=False）；
3. 达到目标天数只代表「观察天数够」，``days_met`` 不等于策略通过。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import ForwardObservation
from app.market_rules.calendar import TradingCalendar
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))

#: A 股收盘时间（北京时间）：只有过了这个时点，当天才算一个完整观察日
_CLOSE_TIME = time(15, 0)

#: 当前工程冻结标识。**改动成交/风控逻辑时必须换新 tag**，旧 tag 的历史保留。
#:
#: 变更历史：
#: * ``2026-09-13-paper-guards-v1`` —— 可成交性护栏（涨跌停/停牌）+ T+1 结算口径；
#: * ``2026-09-14-paper-costs-v1`` —— 模拟盘接入**滑点**（`RISK_SLIPPAGE`，默认 5bp，
#:   与回测 `ExecutionConfig.slippage` 同值），并把买入冻结改为按含滑点上限价计提、
#:   成交后差额退回可用资金；
#: * ``2026-09-14-paper-costs-v2`` —— 再接入**过户费**（`RISK_TRANSFER_FEE_RATE`，
#:   默认 0.001% 双边，与回测 `transfer_fee_rate` 同值），`paper_trades` 新增
#:   `transfer_fee` 列（迁移 0023），冻结额同步覆盖过户费。
#: 两次都是成交逻辑变更 → 按计划重新计时（旧 tag 计数均为 0，未损失观察日）。
CURRENT_FREEZE_TAG = "2026-09-14-paper-costs-v2"

#: 当前标签的计时起点说明（**刻意保守，勿当作 bug 修**）：
#: 起点登记为 **2026-09-14（变更当天）**，而计数规则是「**严格晚于**起点」
#: （见 :meth:`ForwardObservationService.count_day`）→ **当天永远不会被计入**，
#: 首个可计日为下一个交易日 **2026-09-15**（当日 15:30 日终结算时自动 +1）。
#:
#: 这是刻意的读法：新逻辑 2026-09-14 中午才生效，当天上午仍跑旧代码，
#: 因此从「第一个完整处在新逻辑下的交易日」起算比从「变更当天」起算更干净。
#: 该语义已由 `backend/tests/test_forward_observation.py` 的两条用例固化
#: （`test_anchor_set_today_can_never_count_today` /
#:  `test_anchor_yesterday_counts_today_after_close`）。
#: 若将来要让变更当天计入，必须显式把规则改成「含起点日」并同步改测试与文档，
#: **不要**只是把起点回填成更早的日期。


def date_close_time() -> time:
    """当天的收盘时点（便于测试注入与将来改为按公告调整）。"""
    return _CLOSE_TIME


def today_cn() -> date:
    """北京时间今天（前向计时一律用北京时间，不用服务器本地时区或 UTC）。"""
    return datetime.now(_CN_TZ).date()


class ForwardObservationService:
    """前向观察计时的读写入口。"""

    def __init__(self, db: Session):
        self._db = db

    # ──────── 查询 ────────

    def get(self, freeze_tag: str) -> ForwardObservation | None:
        return self._db.scalars(
            select(ForwardObservation).where(ForwardObservation.freeze_tag == freeze_tag)
        ).first()

    def list_all(self) -> list[ForwardObservation]:
        return list(
            self._db.scalars(
                select(ForwardObservation).order_by(ForwardObservation.started_on.desc())
            ).all()
        )

    def status(self, freeze_tag: str | None = None) -> dict:
        """返回单个（默认当前）冻结的进度；不存在时如实返回 registered=False。"""
        tag = freeze_tag or CURRENT_FREEZE_TAG
        row = self.get(tag)
        if row is None:
            return {
                "freeze_tag": tag,
                "registered": False,
                "note": "尚未登记前向观察起点；请调用 start 明确锚点，不能用历史重放代替。",
            }
        return self._serialize(row)

    @staticmethod
    def _serialize(row: ForwardObservation) -> dict:
        return {
            "freeze_tag": row.freeze_tag,
            "registered": True,
            "started_on": row.started_on.isoformat(),
            "target_trading_days": row.target_trading_days,
            "trading_days_counted": row.trading_days_counted,
            "remaining_trading_days": row.remaining_trading_days,
            "days_met": row.days_met,
            "last_counted_day": row.last_counted_day.isoformat() if row.last_counted_day else None,
            "account_id": row.account_id,
            "baseline_equity": row.baseline_equity,
            "current_equity": row.current_equity,
            "notes": row.notes,
            "as_of": today_cn().isoformat(),
            "caveat": "达到目标天数只表示观察期足够，不代表策略通过；仍需样本外与对账门槛。",
        }

    # ──────── 写入 ────────

    def start(
        self,
        *,
        freeze_tag: str = CURRENT_FREEZE_TAG,
        started_on: date | None = None,
        target_trading_days: int = 60,
        account_id: int | None = None,
        baseline_equity: float | None = None,
        notes: str = "",
    ) -> dict:
        """登记一次工程冻结的计时起点（按 freeze_tag 幂等）。"""
        anchor = started_on or today_cn()
        existing = self.get(freeze_tag)
        if existing is not None:
            return {"created": False, **self._serialize(existing)}
        row = ForwardObservation(
            freeze_tag=freeze_tag,
            started_on=anchor,
            target_trading_days=target_trading_days,
            account_id=account_id,
            baseline_equity=baseline_equity,
            notes=notes,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info("前向观察起点已登记：%s @ %s（目标 %s 个交易日）", freeze_tag, anchor, target_trading_days)
        return {"created": True, **self._serialize(row)}

    def count_day(
        self,
        trading_date: date | None = None,
        *,
        freeze_tag: str = CURRENT_FREEZE_TAG,
        equity: float | None = None,
    ) -> dict:
        """把某个交易日计入前向观察（幂等、拒绝未来日与非交易日）。"""
        target = trading_date or today_cn()
        row = self.get(freeze_tag)
        if row is None:
            return {
                "counted": False,
                "reason": "尚未登记前向观察起点（freeze_tag=%s）" % freeze_tag,
                "freeze_tag": freeze_tag,
            }
        if target > today_cn():
            return {"counted": False, "reason": f"{target.isoformat()} 晚于今天，不接受未来日", "freeze_tag": freeze_tag}
        # 只有**已经收盘**的交易日才算一个完整观察日：否则当天开盘前就能把「今天」
        # 计入，等于用未完成的日子充数（实测踩到：早上 08:46 时当天已被允许计入）。
        now = datetime.now(_CN_TZ)
        if target == now.date() and now.time() < date_close_time():
            return {
                "counted": False,
                "reason": (
                    f"{target.isoformat()} 尚未收盘（北京时间 {now.strftime('%H:%M')}），"
                    "完整交易日后才能计入前向观察"
                ),
                "freeze_tag": freeze_tag,
            }
        if target <= row.started_on:
            return {
                "counted": False,
                "reason": f"{target.isoformat()} 不晚于起点 {row.started_on.isoformat()}",
                "freeze_tag": freeze_tag,
            }
        if row.last_counted_day is not None and target <= row.last_counted_day:
            return {
                "counted": False,
                "reason": f"{target.isoformat()} 已计入（最后计数日 {row.last_counted_day.isoformat()}）",
                "freeze_tag": freeze_tag,
            }
        calendar = TradingCalendar(self._db)
        if not calendar.is_empty() and not calendar.is_trading_day(target):
            return {"counted": False, "reason": f"{target.isoformat()} 非交易日", "freeze_tag": freeze_tag}

        row.trading_days_counted += 1
        row.last_counted_day = target
        if equity is not None:
            row.current_equity = equity
            if row.baseline_equity is None:
                row.baseline_equity = equity
        row.updated_at = utc_now()
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "前向观察计数：%s +1 → %s/%s（%s）",
            freeze_tag,
            row.trading_days_counted,
            row.target_trading_days,
            target.isoformat(),
        )
        return {"counted": True, **self._serialize(row)}
