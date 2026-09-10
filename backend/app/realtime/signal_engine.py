"""信号处理引擎。

实时检测管道：行情输入 → 更新缓存 → 执行启用的策略 → 信号冷却去重
→ 保存信号 → WebSocket 推送。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.database.models import Signal as SignalModel
from app.market_data.base import QuoteData
from app.realtime.quote_cache import QuoteCache
from app.realtime.websocket_manager import ConnectionManager
from app.strategies.base import Signal, Strategy
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


class SignalEngine:
    """信号处理引擎。"""

    def __init__(
        self,
        quote_cache: QuoteCache,
        connection_manager: ConnectionManager,
        get_enabled_strategies,
        cooldown_seconds: float = 60.0,
    ):
        self._cache = quote_cache
        self._ws = connection_manager
        self._get_enabled_strategies = get_enabled_strategies
        self._cooldown = cooldown_seconds
        # 冷却状态：key -> 上次触发时间戳
        self._cooldown_map: dict[tuple, float] = {}

    async def process_quotes(self, quotes: dict[str, QuoteData]) -> list[Signal]:
        """处理新行情，生成并返回信号（同时推送与落库）。"""
        strategies = self._get_enabled_strategies()
        if not strategies:
            return []

        signals: list[Signal] = []
        for symbol, quote in quotes.items():
            window = self._cache.get_window(symbol)
            if len(window) < 2:
                continue
            for strategy in strategies:
                try:
                    signal = strategy.analyze(window)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("策略 %s 分析 %s 失败: %s", strategy.name, symbol, exc)
                    continue
                if signal is None:
                    continue
                if not self._check_cooldown(signal):
                    continue
                self._save_signal(signal)
                signals.append(signal)
                await self._ws.broadcast_signal(signal.model_dump(mode="json"))
        return signals

    def _check_cooldown(self, signal: Signal) -> bool:
        """冷却去重：相同股票+策略+方向在冷却期内不重复触发。"""
        key = (signal.symbol, signal.strategy_name, signal.direction)
        now = time.time()
        last = self._cooldown_map.get(key)
        if last is not None and now - last < self._cooldown:
            return False
        self._cooldown_map[key] = now
        return True

    def _save_signal(self, signal: Signal) -> None:
        """将信号落库。"""
        from app.database.session import SessionLocal

        db: Session = SessionLocal()
        try:
            record = SignalModel(
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                direction=signal.direction,
                strength=signal.strength,
                reason=signal.reason,
                price=signal.price,
                source_time=signal.source_time or utc_now(),
                strategy_version=signal.strategy_version,
            )
            db.add(record)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.error("保存信号失败: %s", exc)
        finally:
            db.close()
