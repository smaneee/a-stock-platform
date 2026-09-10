"""AKShare 行情数据源。

AKShare 提供历史数据和备用实时数据。属于可选依赖，未安装时优雅降级。
历史数据接口为同步阻塞调用，需放入线程池执行以免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from app.market_data.base import MarketDataProvider, QuoteData
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

try:
    import akshare as ak  # type: ignore

    _AKSHARE_AVAILABLE = True
except ImportError:  # pragma: no cover
    ak = None  # type: ignore
    _AKSHARE_AVAILABLE = False


class AkshareProvider(MarketDataProvider):
    """AKShare 数据源（历史数据为主）。"""

    name = "akshare"

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        # AKShare 实时行情接口较慢，第一版主要用作历史数据，实时行情由腾讯承担
        return {}

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        if not _AKSHARE_AVAILABLE:
            logger.warning("AKShare 未安装，无法获取历史行情")
            return []

        def _fetch() -> list[QuoteData]:
            try:
                period_map = {"1m": "1", "5m": "5", "daily": "daily", "weekly": "weekly"}
                ak_period = period_map.get(period, "daily")
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period=ak_period,
                    start_date=start_time.strftime("%Y%m%d"),
                    end_date=end_time.strftime("%Y%m%d"),
                    adjust="",
                )
                quotes: list[QuoteData] = []
                for _, row in df.iterrows():
                    quotes.append(
                        QuoteData(
                            symbol=symbol,
                            name=symbol,
                            price=float(row.get("收盘", 0) or 0),
                            open=float(row.get("开盘", 0) or 0),
                            high=float(row.get("最高", 0) or 0),
                            low=float(row.get("最低", 0) or 0),
                            previous_close=0.0,
                            volume=float(row.get("成交量", 0) or 0),
                            amount=float(row.get("成交额", 0) or 0),
                            bid_price=0.0,
                            ask_price=0.0,
                            source=self.name,
                            market_time=datetime.strptime(str(row.get("日期")), "%Y-%m-%d"),
                            received_at=utc_now(),
                            is_stale=False,
                        )
                    )
                return quotes
            except Exception as exc:  # noqa: BLE001
                logger.warning("AKShare 获取历史行情失败: %s", exc)
                return []

        return await asyncio.to_thread(_fetch)

    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        return None

    async def health_check(self) -> bool:
        return _AKSHARE_AVAILABLE
