"""Read-only background reconciliation for submitted live QMT plans."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.database.models import LiveRebalancePlan
from app.database.session import SessionLocal
from app.live_trading.qmt_broker import QmtBrokerError, QmtLiveBroker
from app.live_trading.rebalance import LiveRebalanceError, LiveRebalanceService
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("SUBMITTED", "PARTIAL")


class LiveReconcileWorker:
    """Poll QMT order state for already-submitted live plans.

    This worker is intentionally read-only: it never places, retries, or cancels
    orders. It only updates local execution_json with broker-reported state.
    """

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        settings: Settings | None = None,
        broker_factory: Callable[[], QmtLiveBroker] | None = None,
    ):
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._broker_factory = broker_factory or (lambda: QmtLiveBroker(self._settings))
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="live-reconcile-worker"
        )
        logger.info("实盘委托对账 worker 已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("实盘委托对账 worker 已停止")

    async def _poll_loop(self) -> None:
        interval = max(float(self._settings.live_reconcile_interval_seconds), 5.0)
        while self._running:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("实盘委托对账 worker 异常: %s", exc)
            await asyncio.sleep(interval)

    async def reconcile_once(self) -> int:
        if not self._is_configured():
            return 0
        plan_ids = self._active_plan_ids()
        if not plan_ids:
            return 0
        broker = self._broker_factory()
        try:
            await broker.connect()
            orders = await broker.query_orders()
            updated = 0
            for plan_id in plan_ids:
                if self._reconcile_plan(plan_id, orders):
                    updated += 1
            return updated
        except QmtBrokerError as exc:
            self._record_reconcile_error(plan_ids, str(exc))
            logger.warning("实盘委托对账失败: %s", exc)
            return 0
        finally:
            await broker.close()

    def _is_configured(self) -> bool:
        return bool(
            self._settings.real_trading_enabled
            and self._settings.qmt_userdata_path
            and self._settings.qmt_account_id
        )

    def _active_plan_ids(self) -> list[int]:
        db = self._session_factory()
        try:
            account_fingerprint = LiveRebalanceService(
                db, self._settings.qmt_account_id
            ).account_fingerprint
            return list(
                db.scalars(
                    select(LiveRebalancePlan.id)
                    .where(LiveRebalancePlan.account_fingerprint == account_fingerprint)
                    .where(LiveRebalancePlan.status.in_(_ACTIVE_STATUSES))
                    .order_by(LiveRebalancePlan.id)
                ).all()
            )
        finally:
            db.close()

    def _reconcile_plan(self, plan_id: int, orders) -> bool:
        db = self._session_factory()
        try:
            service = LiveRebalanceService(db, self._settings.qmt_account_id)
            plan = service.reconcile(plan_id, orders)
            logger.info("实盘方案 #%s 对账完成: status=%s", plan.id, plan.status)
            return True
        except LiveRebalanceError as exc:
            logger.warning("实盘方案 #%s 对账跳过: %s", plan_id, exc)
            return False
        finally:
            db.close()

    def _record_reconcile_error(self, plan_ids: list[int], message: str) -> None:
        db = self._session_factory()
        try:
            now = utc_now().isoformat()
            for plan_id in plan_ids:
                plan = db.get(LiveRebalancePlan, plan_id)
                if plan is None or plan.status not in _ACTIVE_STATUSES:
                    continue
                execution = dict(plan.execution_json or {})
                execution["last_reconcile_error"] = message
                execution["last_reconcile_error_at"] = now
                plan.execution_json = execution
                plan.error_message = message
            db.commit()
        finally:
            db.close()
