"""研究复核提醒的定时扫描。

为什么需要：复核本身是只读接口，但"要记得去点一次"这件事不该由人负责。研究记录冻结
之后，**新财报、价格偏离、结论变化、数据过期、记录陈旧**这五种情况都会让旧结论失去
前提；这里在每个交易日收盘后（默认 15:45，日终结算 15:30 之后）自动扫描一次，
把触发的条件写进 ``research_review_reminders``，形成可确认的待办列表。

安全边界：只读行情/快照与冻结研究记录，只写平台自己的提醒表，**不涉及任何交易动作**，
也不修改研究记录，因此默认开启；需要关闭时设置 ``RESEARCH_REMINDER_AUTO_ENABLED=false``。
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.database.session import SessionLocal
from app.research.service import scan_reminders

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_JOB_ID = "research_review_reminder_scan"


class ResearchReminderScheduler:
    """按配置在收盘后扫描研究记录并落库复核提醒。"""

    def __init__(self, db_factory=None, settings: Settings | None = None):
        self._db_factory = db_factory or SessionLocal
        self._settings = settings or get_settings()
        self._scheduler = AsyncIOScheduler(timezone=_CN_TZ)

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def describe(self) -> dict:
        """供 API / 前端展示的调度配置（只读）。"""
        next_run = None
        if self.is_running:
            job = self._scheduler.get_job(_JOB_ID)
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time.isoformat()
        settings = self._settings
        return {
            "enabled": bool(settings.research_reminder_auto_enabled),
            "hour": settings.research_reminder_auto_hour,
            "minute": settings.research_reminder_auto_minute,
            "scan_limit": settings.research_reminder_scan_limit,
            "running": self.is_running,
            "next_run_at": next_run,
            "scope": "只读快照与冻结研究记录，只写提醒表；不改研究记录、不下单",
        }

    def start(self) -> None:
        if not self._settings.research_reminder_auto_enabled:
            logger.info("研究复核提醒自动扫描未启用（RESEARCH_REMINDER_AUTO_ENABLED=false）")
            return
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self._run_safe,
            CronTrigger(
                hour=self._settings.research_reminder_auto_hour,
                minute=self._settings.research_reminder_auto_minute,
                timezone=_CN_TZ,
            ),
            id=_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        logger.info(
            "研究复核提醒自动扫描已启动：每日 %02d:%02d（北京时间）",
            self._settings.research_reminder_auto_hour,
            self._settings.research_reminder_auto_minute,
        )

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("研究复核提醒自动扫描已关闭")

    async def _run_safe(self) -> None:
        """调度器回调用安全包装：异常不能抛回事件循环。"""
        try:
            await self.run_once()
        except Exception as exc:  # noqa: BLE001 - 调度任务必须自我兜底
            logger.exception("研究复核提醒扫描失败：%s", exc)

    async def run_once(self) -> dict:
        """扫描一次并返回统计（幂等：同一天重复扫描不产生重复提醒）。"""
        db = self._db_factory()
        try:
            result = scan_reminders(db, limit=self._settings.research_reminder_scan_limit)
            logger.info(
                "研究复核提醒扫描完成：标的 %s 个，新建提醒 %s 条，跳过 %s 个",
                result["symbols_scanned"],
                result["reminders_created"],
                len(result["skipped"]),
            )
            return result
        finally:
            db.close()
