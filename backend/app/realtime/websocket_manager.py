"""WebSocket 连接管理。

管理客户端连接与订阅关系，推送行情与信号。
内置慢客户端保护：每个连接使用有界队列，队列满时丢弃最旧消息，
避免慢客户端导致内存无限增长。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from fastapi import WebSocket

from app.config import get_settings
from app.market_data.base import QuoteData

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class _Connection:
    """单个 WebSocket 连接的内部状态。"""

    websocket: WebSocket
    queue: asyncio.Queue
    channel: str = "quotes"  # quotes / signals / all
    symbols: set[str] = field(default_factory=set)
    max_subscriptions: int = 200

    def can_subscribe(self, new_symbols: set[str]) -> bool:
        merged = self.symbols | new_symbols
        return len(merged) <= self.max_subscriptions


class ConnectionManager:
    """WebSocket 连接管理器。"""

    def __init__(self, queue_size: int = 1000):
        self._connections: set[_Connection] = set()
        self._lock = asyncio.Lock()
        self._queue_size = queue_size
        self._max_subscriptions = settings.ws_max_subscriptions

    async def connect(self, websocket: WebSocket, channel: str = "quotes") -> None:
        await websocket.accept()
        conn = _Connection(
            websocket=websocket,
            queue=asyncio.Queue(maxsize=self._queue_size),
            channel=channel,
            max_subscriptions=self._max_subscriptions,
        )
        async with self._lock:
            self._connections.add(conn)
        asyncio.create_task(self._sender(conn))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections = {c for c in self._connections if c.websocket is not websocket}

    async def subscribe(self, websocket: WebSocket, symbols: list[str]) -> bool:
        """订阅股票，返回是否成功。"""
        new_symbols = set(symbols)
        async with self._lock:
            for conn in self._connections:
                if conn.websocket is websocket:
                    if not conn.can_subscribe(new_symbols):
                        logger.warning("WebSocket 订阅数量超限")
                        return False
                    conn.symbols |= new_symbols
                    return True
        return False

    async def broadcast_quote(self, quote: QuoteData) -> None:
        """推送行情给订阅了该股票的行情连接。"""
        await self._broadcast(
            {"type": "quote", "data": quote.model_dump(mode="json")},
            predicate=lambda conn: conn.channel in ("quotes", "all")
            and quote.symbol in conn.symbols,
        )

    async def broadcast_signal(self, signal: dict) -> None:
        """推送信号给信号连接。"""
        await self._broadcast(
            {"type": "signal", "data": signal},
            predicate=lambda conn: conn.channel in ("signals", "all"),
        )

    async def _broadcast(self, message: dict, predicate) -> None:
        async with self._lock:
            connections = list(self._connections)
        text = json.dumps(message, ensure_ascii=False, default=str)
        for conn in connections:
            try:
                if not predicate(conn):
                    continue
                try:
                    conn.queue.put_nowait(text)
                except asyncio.QueueFull:
                    try:
                        conn.queue.get_nowait()
                        conn.queue.put_nowait(text)
                    except asyncio.QueueFull:
                        pass
            except Exception:  # noqa: BLE001
                continue

    async def _sender(self, conn: _Connection) -> None:
        """每个连接的发送循环。"""
        try:
            while True:
                text = await conn.queue.get()
                await conn.websocket.send_text(text)
        except Exception:  # noqa: BLE001 - 客户端断开
            pass
        finally:
            await self.disconnect(conn.websocket)
