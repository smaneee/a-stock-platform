"""QMT / xtdata 行情数据源（可选，正式实时行情）。

QMT（迅投）提供正式实时行情与推送模式，目标延迟不超过 1 秒。
依赖 xtdata 客户端，未安装或未登录时优雅降级，由其它数据源兜底。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Callable

from app.market_data.base import MarketDataProvider, QuoteData

logger = logging.getLogger(__name__)

try:
    from xtquant import xtdata  # type: ignore

    _QMT_AVAILABLE = True
except ImportError:  # pragma: no cover
    xtdata = None  # type: ignore
    _QMT_AVAILABLE = False


class QmtProvider(MarketDataProvider):
    """QMT 实时行情数据源。"""

    name = "qmt"

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not _QMT_AVAILABLE:
            return {}

        def _qmt_symbol(symbol: str) -> str:
            if "." in symbol:
                return symbol
            suffix = "SH" if symbol.startswith(("5", "6", "9")) else "SZ"
            if symbol.startswith(("4", "8")) or symbol.startswith("92"):
                suffix = "BJ"
            return f"{symbol}.{suffix}"

        def _best_price(value) -> float:
            if isinstance(value, (list, tuple)):
                value = value[0] if value else 0
            return float(value or 0)

        def _fetch() -> dict[str, QuoteData]:
            qmt_symbols = {symbol: _qmt_symbol(symbol) for symbol in symbols}
            data = xtdata.get_full_tick(list(qmt_symbols.values()))
            result: dict[str, QuoteData] = {}
            for symbol, qmt_symbol in qmt_symbols.items():
                tick = (data or {}).get(qmt_symbol) or (data or {}).get(symbol)
                if not tick:
                    continue
                tick_time = tick.get("time")
                market_time = datetime.now(UTC).replace(tzinfo=None)
                if tick_time:
                    timestamp = float(tick_time)
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    market_time = datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
                result[symbol] = QuoteData(
                    symbol=symbol,
                    name=str(tick.get("instrumentName", "")),
                    price=float(tick.get("lastPrice", 0) or 0),
                    open=float(tick.get("open", 0) or 0),
                    high=float(tick.get("high", 0) or 0),
                    low=float(tick.get("low", 0) or 0),
                    previous_close=float(tick.get("lastClose", 0) or 0),
                    volume=float(tick.get("volume", 0) or 0),
                    amount=float(tick.get("amount", 0) or 0),
                    bid_price=_best_price(tick.get("bidPrice")),
                    ask_price=_best_price(tick.get("askPrice")),
                    source=self.name,
                    market_time=market_time,
                    received_at=datetime.now(UTC).replace(tzinfo=None),
                    is_stale=False,
                )
            return result

        try:
            # xtdata 是同步 API，放入线程避免阻塞 FastAPI 事件循环。
            return await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("QMT 获取行情失败: %s", exc)
            return {}

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        return []

    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        # QMT 推送订阅需在同步上下文中注册，第一版留待扩展
        return None

    async def health_check(self) -> bool:
        return _QMT_AVAILABLE
