"""行情数据源管理器。

按优先级管理多个数据源，实现故障转移与本地缓存兜底。
关键约束：
- 严禁静默混合不同来源的数据：一次批量请求只使用单一来源。
- 所有外部源失败时返回最后一次成功数据，并标记 is_stale=True。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.market_data.base import MarketDataProvider, QuoteData

logger = logging.getLogger(__name__)


class ProviderManager:
    """数据源管理器。"""

    def __init__(self, providers: list[MarketDataProvider]):
        # 按传入顺序（即优先级）保存
        self._providers = providers
        # 最后成功数据缓存：symbol -> QuoteData
        self._last_known: dict[str, QuoteData] = {}
        self._lock = asyncio.Lock()

    @property
    def providers(self) -> list[MarketDataProvider]:
        return self._providers

    async def get_quote(self, symbol: str) -> QuoteData | None:
        """获取单只股票行情，失败返回 None。"""
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        """批量获取行情。

        依次尝试各数据源，第一个成功返回完整结果的数据源胜出；
        若全部失败，则返回本地缓存并标记 is_stale=True。
        """
        if not symbols:
            return {}

        for provider in self._providers:
            try:
                result = await provider.get_quotes(symbols)
                if result:
                    # 校验：必须覆盖所有请求的 symbol，否则视为不完整
                    if all(s in result for s in symbols):
                        await self._update_cache(result)
                        return result
                    logger.warning("数据源 %s 返回不完整结果", provider.name)
            except Exception as exc:  # noqa: BLE001 - 数据源异常需兜底
                logger.warning("数据源 %s 获取行情失败: %s", provider.name, exc)
                continue

        # 全部失败，返回缓存
        stale_result = self._build_stale_result(symbols)
        if stale_result:
            logger.warning("所有数据源失败，返回过期缓存数据")
        return stale_result

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        """获取历史行情，按优先级尝试数据源。"""
        for provider in self._providers:
            try:
                data = await provider.get_history(symbol, period, start_time, end_time)
                if data:
                    return data
            except Exception as exc:  # noqa: BLE001
                logger.warning("数据源 %s 获取历史行情失败: %s", provider.name, exc)
                continue
        return []

    async def health_check(self) -> dict[str, bool]:
        """返回各数据源健康状态。"""
        status: dict[str, bool] = {}
        for provider in self._providers:
            try:
                status[provider.name] = await provider.health_check()
            except Exception:  # noqa: BLE001
                status[provider.name] = False
        return status

    async def _update_cache(self, quotes: dict[str, QuoteData]) -> None:
        """更新本地缓存（仅记录非 stale 数据）。"""
        async with self._lock:
            for symbol, quote in quotes.items():
                if quote is not None and not quote.is_stale:
                    self._last_known[symbol] = quote

    def _build_stale_result(self, symbols: list[str]) -> dict[str, QuoteData]:
        """用缓存数据构建 stale 结果。"""
        result: dict[str, QuoteData] = {}
        for symbol in symbols:
            cached = self._last_known.get(symbol)
            if cached is not None:
                stale = cached.model_copy(update={"is_stale": True})
                result[symbol] = stale
        return result
