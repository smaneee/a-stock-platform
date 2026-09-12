"""行情数据抽象层。

定义统一的行情数据结构 QuoteData 与数据源抽象接口 MarketDataProvider，
供腾讯、AKShare、QMT、Mock 等数据源实现，并保证外部源失败时返回 stale 数据。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import math
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field

from app.time_utils import utc_now


class QuoteData(BaseModel):
    """统一行情数据结构。

    所有数据源返回的行情都会被转换为该结构，保证上层逻辑不依赖具体数据源。
    """

    symbol: str = Field(..., description="股票代码，如 600000")
    name: str = Field("", description="股票名称")
    price: float = Field(0.0, description="最新价")
    open: float = Field(0.0, description="开盘价")
    high: float = Field(0.0, description="最高价")
    low: float = Field(0.0, description="最低价")
    previous_close: float = Field(0.0, description="昨收价")
    volume: float = Field(0.0, description="成交量（股）")
    amount: float = Field(0.0, description="成交额（元）")
    bid_price: float = Field(0.0, description="买一价")
    ask_price: float = Field(0.0, description="卖一价")
    source: str = Field("", description="数据来源标识")
    market_time: datetime | None = Field(None, description="行情时间")
    received_at: datetime = Field(default_factory=utc_now, description="接收时间")
    is_stale: bool = Field(False, description="是否为过期缓存数据")

    @property
    def change_percent(self) -> float:
        """涨跌幅（百分比）。昨收为 0 时返回 0。"""
        if not self.previous_close:
            return 0.0
        return (self.price - self.previous_close) / self.previous_close * 100

    def execution_price(self, side: str) -> float:
        """成交参考价：优先对手盘盘口，盘口缺失时按最新价兜底。

        卖 → 买一价，买 → 卖一价；两者为空（东方财富批量行情接口没有五档、
        AKShare 也不提供盘口）时退化为最新价，避免调仓把可买数量算成 0。
        没有任何可用价格时返回 0.0，由调用方按无效行情处理。
        """
        price = self.bid_price if (side or "").upper() == "SELL" else self.ask_price
        if not math.isfinite(price) or price <= 0:
            price = self.price
        if not math.isfinite(price) or price <= 0:
            return 0.0
        return price


class MarketDataProvider(ABC):
    """行情数据源抽象接口。"""

    name: str = "base"
    # 同一上游标识：akshare 的历史接口实际打的就是东财，与 EastmoneyProvider 同源。
    # ProviderManager 用它避免在一次请求里重复打同一个上游；留空按 name 区分。
    upstream: str = ""

    @abstractmethod
    async def get_quote(self, symbol: str) -> QuoteData | None:
        """获取单只股票实时行情，失败返回 None。"""

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        """批量获取行情，返回 symbol -> QuoteData 映射。"""

    @abstractmethod
    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        """获取历史行情数据（K 线）。"""

    @abstractmethod
    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        """订阅行情推送（仅支持推送模式的数据源，如 QMT）。"""

    @abstractmethod
    async def health_check(self) -> bool:
        """数据源健康检查。"""
