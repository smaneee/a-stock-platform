"""定时任务与后台进程管理。

通过 APScheduler 调度 A 股交易日终结算（T+1 解冻 + 资产记录）：
- 每日北京时间 15:30（收盘后）触发；
- 仅对交易日执行；非交易日静默跳过；
- 与 DailySettlement 协作：同一交易日重复执行幂等。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.market_rules.calendar import TradingCalendar, sync_trading_calendar
from app.paper_trading.settlement import DailySettlement

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
CLOSE_HOUR = 15
CLOSE_MINUTE = 30


class SettlementScheduler:
    """APScheduler 驱动的日终结算调度器。"""

    def __init__(self, db_factory=None):
        # db_factory 允许在测试中注入 FakeSession
        self._db_factory = db_factory or SessionLocal
        self._scheduler = AsyncIOScheduler(timezone=_CN_TZ)

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def start(self) -> None:
        """调度每日 15:30（北京时间）触发日终结算。"""
        if self._scheduler.running:
            return
        # 触发器：每日 15:30 北京时间
        self._scheduler.add_job(
            self._run_settlement_safe,
            CronTrigger(
                hour=CLOSE_HOUR,
                minute=CLOSE_MINUTE,
                timezone=_CN_TZ,
            ),
            id="daily_settlement",
            replace_existing=True,
            misfire_grace_time=600,  # 错峰 10 分钟内仍生效
        )
        self._scheduler.start()
        logger.info("SettlementScheduler 已启动，每日 %02d:%02d 执行日终结算", CLOSE_HOUR, CLOSE_MINUTE)

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("SettlementScheduler 已关闭")

    def add_one_off_job(self, fn, run_date) -> None:
        """测试钩子：添加一次性立即触发任务。"""
        self._scheduler.add_job(
            fn,
            "date",
            run_date=run_date,
            timezone=_CN_TZ,
            replace_existing=True,
        )

    async def _run_settlement_safe(self) -> None:
        """同步结算函数的安全包装（调度器在线程池中跑，DB session 不能跨 await）。"""
        try:
            await asyncio.to_thread(self._run_settlement_sync)
        except Exception as exc:  # noqa: BLE001
            logger.exception("日终结算触发异常: %s", exc)

    def _run_settlement_sync(self) -> None:
        db: Session = self._db_factory()
        try:
            calendar = TradingCalendar(db)
            today_cn = datetime.now(_CN_TZ).date()
            if not calendar.is_trading_day(today_cn):
                logger.info("今日 %s 非交易日，跳过日终结算", today_cn)
                return
            settlement = DailySettlement(db)
            results = settlement.settle_all({}, trading_date=today_cn)
            logger.info(
                "日终结算完成：交易日=%s，账户数=%d", today_cn, len(results)
            )
        finally:
            db.close()


def ensure_calendar_ready(db_factory=None) -> dict:
    """启动时调用：若本地交易日历为空，触发同步。

    返回 {source, total, ready}。ready=False 表示仍未同步到任何交易日，
    readiness 探针应据此返回 503。
    """
    factory = db_factory or SessionLocal
    db = factory()
    try:
        calendar = TradingCalendar(db)
        if calendar.is_empty():
            logger.info("本地交易日历为空，触发多源同步")
            result = sync_trading_calendar(db)
            return {
                "source": result.get("source"),
                "total": result.get("total", 0),
                "ready": result.get("total", 0) > 0,
            }
        return {
            "source": "local_cache",
            "total": calendar.count(),
            "ready": True,
        }
    finally:
        db.close()


def sync_calendar_for_tests(db: Session) -> dict:
    """测试钩子：直接同步交易日历（覆盖测试场景）。"""
    return sync_trading_calendar(db)
