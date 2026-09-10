"""策略抽象基类与信号数据模型。"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.market_data.base import QuoteData
from app.time_utils import utc_now


class Signal(BaseModel):
    """策略信号领域模型。

    信号必须附带可解释原因，不能只返回 BUY / SELL。
    """

    signal_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str
    strategy_name: str
    direction: str  # BUY / SELL / ALERT
    strength: float = 1.0
    reason: str
    price: float
    source_time: datetime
    created_at: datetime = Field(default_factory=utc_now)
    strategy_version: str = "1.0.0"


class Strategy(ABC):
    """策略抽象基类。

    每个策略实现 analyze 方法，输入历史行情，输出信号（无信号返回 None）。
    """

    name: str = "base"
    version: str = "1.0.0"
    description: str = ""

    @abstractmethod
    def analyze(self, history: list[QuoteData]) -> Signal | None:
        """分析历史行情，返回最新信号或 None。"""

    def _build_signal(
        self,
        symbol: str,
        direction: str,
        reason: str,
        price: float,
        source_time: datetime,
        strength: float = 1.0,
    ) -> Signal:
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            direction=direction,
            strength=strength,
            reason=reason,
            price=price,
            source_time=source_time,
            strategy_version=self.version,
        )
