"""风控管理器。

对模拟交易订单执行各类风控检查，返回是否允许及原因。
所有检查基于账户快照与行情数据，不依赖具体数据库实现。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.market_data.base import QuoteData
from app.risk.limits import DEFAULT_LIMITS, RiskLimits


@dataclass
class RiskDecision:
    """风控检查结果。"""

    allowed: bool
    reason: str = ""


@dataclass
class AccountSnapshot:
    """账户状态快照，供风控检查使用。"""

    total_asset: float = 0.0
    available_cash: float = 0.0
    initial_cash: float = 0.0
    current_position_value: dict[str, float] = None  # symbol -> 市值
    daily_pnl: float = 0.0
    peak_asset: float = 0.0

    def __post_init__(self):
        if self.current_position_value is None:
            self.current_position_value = {}


class RiskManager:
    """风控管理器。"""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or DEFAULT_LIMITS

    def check_buy(
        self,
        snapshot: AccountSnapshot,
        symbol: str,
        order_value: float,
        quote: QuoteData,
    ) -> RiskDecision:
        """买入订单风控检查。"""
        # 数据过期禁止成交
        if quote.is_stale:
            return RiskDecision(False, "行情数据过期，禁止成交")

        total_asset = snapshot.total_asset
        if total_asset <= 0:
            return RiskDecision(False, "账户总资产异常")

        # 不允许负现金（含手续费）
        commission = self._commission(order_value)
        total_cost = order_value + commission
        if total_cost > snapshot.available_cash:
            return RiskDecision(False, "可用资金不足")

        # 单只股票最大仓位
        existing_value = snapshot.current_position_value.get(symbol, 0.0)
        new_position_value = existing_value + order_value
        if new_position_value / total_asset > self.limits.max_position_per_symbol:
            return RiskDecision(
                False,
                f"单只股票仓位将超过 {self.limits.max_position_per_symbol * 100:.0f}%",
            )

        # 总仓位上限
        total_position_value = sum(snapshot.current_position_value.values()) + order_value
        if total_position_value / total_asset > self.limits.max_total_position:
            return RiskDecision(
                False,
                f"总仓位将超过 {self.limits.max_total_position * 100:.0f}%",
            )

        # 单日最大亏损
        if snapshot.daily_pnl < 0:
            daily_loss = abs(snapshot.daily_pnl) / total_asset
            if daily_loss >= self.limits.max_daily_loss:
                return RiskDecision(
                    False,
                    f"单日亏损已达 {daily_loss * 100:.1f}%，禁止新增买入",
                )

        # 总回撤禁止新增仓位
        if snapshot.peak_asset > 0:
            drawdown = (snapshot.peak_asset - total_asset) / snapshot.peak_asset
            if drawdown >= self.limits.max_total_drawdown:
                return RiskDecision(
                    False,
                    f"总回撤已达 {drawdown * 100:.1f}%，禁止新增仓位",
                )

        return RiskDecision(True)

    def check_sell(
        self,
        snapshot: AccountSnapshot,
        symbol: str,
        quantity: int,
        available_quantity: int,
        quote: QuoteData,
    ) -> RiskDecision:
        """卖出订单风控检查。"""
        if quote.is_stale:
            return RiskDecision(False, "行情数据过期，禁止成交")

        if quantity > available_quantity:
            return RiskDecision(False, "卖出数量超过可用持仓")

        if quantity <= 0:
            return RiskDecision(False, "卖出数量必须大于 0")

        return RiskDecision(True)

    def _commission(self, order_value: float) -> float:
        """计算佣金（含最低收费）。"""
        commission = order_value * self.limits.commission_rate
        return max(commission, self.limits.min_commission)

    def stamp_tax(self, sell_value: float) -> float:
        """计算卖出印花税。"""
        return sell_value * self.limits.stamp_tax_rate
