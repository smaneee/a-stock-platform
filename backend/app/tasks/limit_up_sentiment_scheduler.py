"""涨停板情绪池落库调度。

东财涨停板行情只保留最近若干个交易日，情绪曲线（封板率 / 连板高度）**必须**
每个交易日累积一次，否则过期的历史再也拿不回来。这里用 APScheduler 的
CronTrigger 在北京时间收盘后固定时刻触发一次抓取。

安全边界：只读行情 + 写本地数据库，不涉及任何交易动作，因此默认开启；
需要关闭时设置 ``LIMIT_UP_SENTIMENT_AUTO_ENABLED=false``。
"""
from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.database.session import SessionLocal
from app.market_data.eastmoney_limit_up import EastmoneyLimitUpService
from app.market_data.limit_up_store import LimitUpSentimentStore

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_JOB_ID = "limit_up_sentiment_capture"


class LimitUpSentimentScheduler:
    """按配置在收盘后把当日情绪池落库。"""

    def __init__(
        self,
        db_factory=None,
        service: EastmoneyLimitUpService | None = None,
        settings: Settings | None = None,
    ):
        self._db_factory = db_factory or SessionLocal
        self._settings = settings or get_settings()
        self._service = service or EastmoneyLimitUpService()
        self._scheduler = AsyncIOScheduler(timezone=_CN_TZ)

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def describe(self) -> dict:
        """返回供 API / 前端展示的调度配置（只读）。"""
        next_run = None
        if self.is_running:
            job = self._scheduler.get_job(_JOB_ID)
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time.isoformat()
        settings = self._settings
        return {
            "enabled": bool(settings.limit_up_sentiment_auto_enabled),
            "hour": settings.limit_up_sentiment_auto_hour,
            "minute": settings.limit_up_sentiment_auto_minute,
            "backfill_days": settings.limit_up_sentiment_backfill_days,
            "running": self.is_running,
            "next_run_at": next_run,
        }

    def start(self) -> None:
        """开启每日落库；未启用或已在运行时直接返回。"""
        if not self._settings.limit_up_sentiment_auto_enabled:
            logger.info(
                "涨停板情绪池自动落库未启用（LIMIT_UP_SENTIMENT_AUTO_ENABLED=false）"
            )
            return
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self._run_safe,
            CronTrigger(
                hour=self._settings.limit_up_sentiment_auto_hour,
                minute=self._settings.limit_up_sentiment_auto_minute,
                timezone=_CN_TZ,
            ),
            id=_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        logger.info(
            "涨停板情绪池自动落库已启动：每日 %02d:%02d（北京时间）",
            self._settings.limit_up_sentiment_auto_hour,
            self._settings.limit_up_sentiment_auto_minute,
        )

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("涨停板情绪池自动落库已关闭")

    async def _run_safe(self) -> None:
        """调度器回调用安全包装：异常不能抛回事件循环。"""
        try:
            await self.run_once()
        except Exception as exc:  # noqa: BLE001 - 调度任务必须自我兜底
            logger.exception("涨停板情绪池落库失败：%s", exc)

    async def run_once(
        self, trade_date: date | None = None, backfill_days: int = 0
    ) -> dict:
        """抓取并落库一次；``backfill_days`` > 0 时改为回补最近 N 个自然日。"""
        db = self._db_factory()
        try:
            store = LimitUpSentimentStore(db, self._service)
            if backfill_days > 0:
                results = await store.backfill(backfill_days, end=trade_date)
                return {"captured": len(results), "mode": "backfill"}
            result = await store.capture(trade_date)
            return {"captured": 0 if result is None else 1, "mode": "capture"}
        finally:
            db.close()
