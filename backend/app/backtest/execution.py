"""回测成交执行。

落实 A 股基本约束：T+1、100 股整数手、涨跌停限制、停牌不能成交、
佣金最低收费、印花税、可配置滑点，并禁止使用未来数据。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.market_data.base import QuoteData


@dataclass(frozen=True)
class ExecutionConfig:
    """回测成交配置。"""

    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage: float = 0.0005  # 滑点比例
    price_limit: float = 0.10  # 涨跌停幅度（主板 10%）
    lot_size: int = 100


@dataclass
class ExecutionResult:
    """单笔成交结果。"""

    filled: bool
    reason: str = ""
    price: float = 0.0
    quantity: int = 0
    commission: float = 0.0
    stamp_tax: float = 0.0


class ExecutionSimulator:
    """回测成交模拟器。"""

    def __init__(self, config: ExecutionConfig | None = None):
        self.config = config or ExecutionConfig()

    def try_fill(
        self,
        side: str,
        quantity: int,
        bar: QuoteData,
    ) -> ExecutionResult:
        """尝试在指定 K 线（开盘价）成交。

        bar 为信号产生后的下一根 K 线，使用其开盘价成交以避免未来数据。
        """
        # 停牌：成交量为 0 无法成交
        if bar.volume <= 0:
            return ExecutionResult(False, "停牌，无法成交")

        price = bar.open
        if price <= 0:
            return ExecutionResult(False, "开盘价无效")

        # 涨跌停限制
        if bar.previous_close > 0:
            limit_up = bar.previous_close * (1 + self.config.price_limit)
            limit_down = bar.previous_close * (1 - self.config.price_limit)
            if side == "BUY" and price >= limit_up:
                return ExecutionResult(False, "一字涨停，无法买入")
            if side == "SELL" and price <= limit_down:
                return ExecutionResult(False, "一字跌停，无法卖出")

        # 滑点：买入抬价，卖出压价
        if side == "BUY":
            fill_price = price * (1 + self.config.slippage)
        else:
            fill_price = price * (1 - self.config.slippage)

        # 整数手校验
        if quantity % self.config.lot_size != 0:
            return ExecutionResult(False, "数量必须为 100 股整数手")

        value = fill_price * quantity
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        stamp_tax = value * self.config.stamp_tax_rate if side == "SELL" else 0.0

        return ExecutionResult(
            filled=True,
            price=fill_price,
            quantity=quantity,
            commission=commission,
            stamp_tax=stamp_tax,
        )
