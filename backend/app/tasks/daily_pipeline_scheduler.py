"""每日流水线自动调度（可选）。

与 SettlementScheduler 一致，用 APScheduler 的 CronTrigger 在北京时间固定
时刻触发；只在 A 股交易日创建当日流水线任务，之后由 DailyPipelineWorker
负责推进（股票池 → 历史入库 → 选股 → 模拟调仓草案）。

安全边界：
- 默认关闭（DAILY_PIPELINE_AUTO_ENABLED=false），不会在用户不知情时跑任务。
- 只创建任务，永不触发真实下单；真实 QMT 通道只能由用户在界面上确认后提交。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.database.session import SessionLocal
from app.market_rules.calendar import TradingCalendar
from app.tasks.daily_pipeline import DailyPipelineError, DailyPipelineService

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_JOB_ID = "daily_pipeline_auto"


class DailyPipelineScheduler:
    """按配置在交易日固定时刻创建当日流水线任务。"""

    def __init__(self, db_factory=None, provider_manager=None, settings: Settings | None = None):
        self._db_factory = db_factory or SessionLocal
        self._provider_manager = provider_manager
        self._settings = settings or get_settings()
        self._scheduler = AsyncIOScheduler(timezone=_CN_TZ)

    @property
    def is_running(self) -> bool:
        return self._scheduler.running

    def describe(self) -> dict:
        """返回供 API/前端展示的调度配置（只读）。"""
        settings = self._settings
        next_run = None
        if self.is_running:
            job = self._scheduler.get_job(_JOB_ID)
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time.isoformat()
        return {
            "enabled": bool(settings.daily_pipeline_auto_enabled),
            "hour": settings.daily_pipeline_auto_hour,
            "minute": settings.daily_pipeline_auto_minute,
            "paper_account_id": settings.daily_pipeline_auto_paper_account_id or None,
            "auto_execute_paper": bool(settings.daily_pipeline_auto_execute_paper),
            "lookback_days": settings.daily_pipeline_auto_lookback_days,
            "running": self.is_running,
            "next_run_at": next_run,
        }

    def start(self) -> None:
        """开启自动调度；未启用或已在运行时直接返回。"""
        if not self._settings.daily_pipeline_auto_enabled:
            logger.info("每日流水线自动调度未启用（DAILY_PIPELINE_AUTO_ENABLED=false）")
            return
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self._run_safe,
            CronTrigger(
                hour=self._settings.daily_pipeline_auto_hour,
                minute=self._settings.daily_pipeline_auto_minute,
                timezone=_CN_TZ,
            ),
            id=_JOB_ID,
            replace_existing=True,
            misfire_grace_time=1800,
        )
        self._scheduler.start()
        logger.info(
            "每日流水线自动调度已启动：交易日 %02d:%02d（北京时间）",
            self._settings.daily_pipeline_auto_hour,
            self._settings.daily_pipeline_auto_minute,
        )

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("每日流水线自动调度已关闭")

    async def _run_safe(self) -> None:
        """调度器回调用安全包装：DB session 不能跨 await，放线程里跑。"""
        try:
            await asyncio.to_thread(self.create_run_for)
        except Exception as exc:  # noqa: BLE001 - 调度任务不能把异常抛回事件循环
            logger.exception("每日流水线自动调度执行异常: %s", exc)

    def create_run_for(self, trading_day: date | None = None):
        """为非交易日返回 None；否则创建（或复用）当日流水线任务。"""
        settings = self._settings
        db = self._db_factory()
        try:
            day = trading_day or datetime.now(_CN_TZ).date()
            if not TradingCalendar(db).is_trading_day(day):
                logger.info("自动调度：%s 非交易日，跳过每日流水线", day)
                return None
            account_id = settings.daily_pipeline_auto_paper_account_id or None
            auto_execute = bool(settings.daily_pipeline_auto_execute_paper)
            if auto_execute and account_id is None:
                logger.warning(
                    "自动调度：未配置 DAILY_PIPELINE_AUTO_PAPER_ACCOUNT_ID，"
                    "已忽略自动执行模拟调仓"
                )
                auto_execute = False
            try:
                run = DailyPipelineService(
                    db, provider_manager=self._provider_manager
                ).create_run(
                    day,
                    paper_account_id=account_id,
                    auto_execute_paper=auto_execute,
                    lookback_days=settings.daily_pipeline_auto_lookback_days,
                )
            except DailyPipelineError as exc:
                logger.warning("自动调度：创建每日流水线失败：%s", exc)
                return None
            logger.info(
                "自动调度：已创建每日流水线 #%s（交易日=%s，模拟账户=%s，自动执行=%s）",
                run.id,
                run.trading_day,
                account_id,
                auto_execute,
            )
            return run
        finally:
            db.close()
