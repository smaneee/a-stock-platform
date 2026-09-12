"""行情轮询调度器。

按固定间隔（免费源默认 3 秒）轮询自选股行情，执行：
行情输入 → 数据校验 → 更新内存缓存 → 推送行情 → 触发策略处理。
只监控自选股，不抓取全部 A 股。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.market_data.base import QuoteData
from app.market_data.provider_manager import ProviderManager
from app.realtime.quote_cache import QuoteCache
from app.realtime.websocket_manager import ConnectionManager

logger = logging.getLogger(__name__)
settings = get_settings()


class QuoteScheduler:
    """定时轮询行情的调度器。"""

    def __init__(
        self,
        provider_manager: ProviderManager,
        quote_cache: QuoteCache,
        connection_manager: ConnectionManager,
        get_symbols: Callable[[], list[str]],
        on_quotes: Callable[[dict[str, QuoteData]], Coroutine] | None = None,
    ):
        self._provider_manager = provider_manager
        self._cache = quote_cache
        self._ws = connection_manager
        self._get_symbols = get_symbols
        self._on_quotes = on_quotes
        self._scheduler = AsyncIOScheduler()
        self._running = False

    def start(self) -> None:
        """启动定时任务。"""
        if self._running:
            return
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=settings.quote_poll_interval,
            id="quote_poll",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        self._running = True
        logger.info("行情轮询已启动，间隔 %.1f 秒", settings.quote_poll_interval)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._running = False

    @property
    def is_running(self) -> bool:
        """是否在运行（供健康检查查询）。"""
        return self._running and self._scheduler.running

    async def poll_once(self) -> dict[str, QuoteData]:
        """执行一次轮询。"""
        symbols = self._get_symbols()
        if not symbols:
            return {}

        quotes = await self._provider_manager.get_quotes(symbols)
        valid_quotes: dict[str, QuoteData] = {}

        for symbol, quote in quotes.items():
            if quote is None:
                continue
            if not self._validate(quote):
                logger.warning("跳过非法行情: %s", symbol)
                continue
            # 过期行情可以推送给前端展示断流状态，但不能进入指标和策略管道。
            if quote.is_stale:
                await self._ws.broadcast_quote(quote)
                continue
            self._cache.update(quote)
            valid_quotes[symbol] = quote
            await self._ws.broadcast_quote(quote)

        # 触发策略处理管道
        if self._on_quotes and valid_quotes:
            try:
                await self._on_quotes(valid_quotes)
            except Exception as exc:  # noqa: BLE001
                logger.error("策略处理失败: %s", exc)

        return valid_quotes

    @staticmethod
    def _validate(quote: QuoteData) -> bool:
        """行情数据校验。"""
        if not quote.symbol:
            return False
        if quote.price <= 0:
            return False
        # 价格与高低价应基本自洽（容忍小数误差）
        if quote.high > 0 and quote.low > 0 and quote.high < quote.low:
            return False
        return True
