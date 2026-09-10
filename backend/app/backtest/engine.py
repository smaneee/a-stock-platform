"""回测引擎。

逐根 K 线推进，信号产生后只能在下一根 K 线开盘价成交，杜绝未来数据。
实现 T+1（当日买入次日可卖）、100 股整数手、涨跌停、停牌、手续费等约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.backtest import metrics
from app.backtest.execution import ExecutionConfig, ExecutionSimulator
from app.market_data.base import QuoteData
from app.strategies.base import Signal, Strategy


@dataclass
class BacktestResult:
    """回测结果。"""

    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    trade_count: int = 0
    equity_curve: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "win_rate": self.win_rate,
            "profit_loss_ratio": self.profit_loss_ratio,
            "trade_count": self.trade_count,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }


class BacktestEngine:
    """回测引擎。"""

    def __init__(
        self,
        strategy: Strategy,
        initial_cash: float = 100_000.0,
        config: ExecutionConfig | None = None,
        analysis_window: int = 300,
    ):
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.simulator = ExecutionSimulator(config)
        self.analysis_window = max(60, analysis_window)

    def run(self, history: list[QuoteData]) -> BacktestResult:
        """运行回测，返回结果。"""
        if not history:
            return BacktestResult()

        history = sorted(history, key=lambda item: item.market_time or item.received_at)

        cash = self.initial_cash
        position = 0  # 总持仓
        available_position = 0  # 可卖持仓（已解冻）
        today_bought = 0  # 当日买入（次日解冻）
        avg_cost = 0.0
        pending_signal: Signal | None = None
        equity_curve: list[float] = []
        trades: list[dict] = []
        closed_trades: list[dict] = []  # 已平仓，用于胜率/盈亏比
        previous_trading_date = None

        for i, bar in enumerate(history):
            trading_date = (bar.market_time or bar.received_at).date()
            # T+1 按交易日解冻，分钟回测不能在下一分钟提前解冻。
            if previous_trading_date is not None and trading_date != previous_trading_date:
                available_position += today_bought
                today_bought = 0

            # 1) 处理上一根 K 线产生的 pending 信号，在本根开盘价成交
            if pending_signal is not None:
                cash, position, available_position, today_bought, avg_cost, realized = (
                    self._execute(pending_signal, bar, cash, position, available_position, today_bought, avg_cost)
                )
                trades.extend(realized["trades"])
                closed_trades.extend(realized["closed"])
                pending_signal = None

            # 2) 用截至本根的历史数据生成信号（收盘后才知道）
            start_index = max(0, i + 1 - self.analysis_window)
            signal = self.strategy.analyze(history[start_index : i + 1])
            if signal is not None:
                pending_signal = signal

            # 3) 记录资金曲线（按收盘价估值）
            total_asset = cash + position * bar.price
            equity_curve.append(total_asset)
            previous_trading_date = trading_date

        result = BacktestResult(equity_curve=equity_curve, trades=trades)
        result.trade_count = len(trades)

        final_asset = equity_curve[-1] if equity_curve else self.initial_cash
        trading_days = len(history)

        result.total_return = metrics.total_return(final_asset, self.initial_cash)
        result.annual_return = metrics.annual_return(final_asset, self.initial_cash, trading_days)
        result.max_drawdown = metrics.max_drawdown(equity_curve)
        result.sharpe_ratio = metrics.sharpe_ratio(equity_curve)
        result.win_rate = metrics.win_rate(closed_trades)
        result.profit_loss_ratio = metrics.profit_loss_ratio(closed_trades)
        return result

    def _execute(
        self,
        signal: Signal,
        bar: QuoteData,
        cash: float,
        position: int,
        available_position: int,
        today_bought: int,
        avg_cost: float,
    ) -> tuple[float, int, int, int, float, dict]:
        """执行信号，返回更新后的账户状态与成交记录。"""
        realized: dict = {"trades": [], "closed": []}

        if signal.direction == "BUY":
            # 按 100 股整数手，用可用现金买入
            estimated_price = bar.open * (1 + self.simulator.config.slippage)
            affordable = int(cash // (estimated_price * 100)) * 100 if estimated_price > 0 else 0
            while affordable >= 100:
                value = estimated_price * affordable
                commission = max(
                    value * self.simulator.config.commission_rate,
                    self.simulator.config.min_commission,
                )
                if value + commission <= cash:
                    break
                affordable -= 100
            if affordable < 100:
                return cash, position, available_position, today_bought, avg_cost, realized

            result = self.simulator.try_fill("BUY", affordable, bar)
            if not result.filled:
                return cash, position, available_position, today_bought, avg_cost, realized

            cost = result.price * result.quantity + result.commission
            cash -= cost
            new_quantity = position + result.quantity
            avg_cost = (avg_cost * position + cost) / new_quantity if new_quantity else 0.0
            position = new_quantity
            today_bought += result.quantity  # T+1 冻结

            realized["trades"].append(
                {
                    "time": (bar.market_time or bar.received_at).isoformat(),
                    "symbol": bar.symbol,
                    "side": "BUY",
                    "price": result.price,
                    "quantity": result.quantity,
                    "commission": result.commission,
                    "pnl": 0.0,
                }
            )

        elif signal.direction == "SELL":
            if available_position < 100:
                return cash, position, available_position, today_bought, avg_cost, realized

            sell_qty = (available_position // 100) * 100
            result = self.simulator.try_fill("SELL", sell_qty, bar)
            if not result.filled:
                return cash, position, available_position, today_bought, avg_cost, realized

            proceeds = result.price * result.quantity - result.commission - result.stamp_tax
            cash += proceeds
            pnl = (result.price - avg_cost) * result.quantity - result.commission - result.stamp_tax
            position -= result.quantity
            available_position -= result.quantity
            if position == 0:
                avg_cost = 0.0

            realized["trades"].append(
                {
                    "time": (bar.market_time or bar.received_at).isoformat(),
                    "symbol": bar.symbol,
                    "side": "SELL",
                    "price": result.price,
                    "quantity": result.quantity,
                    "commission": result.commission,
                    "stamp_tax": result.stamp_tax,
                    "pnl": pnl,
                }
            )
            realized["closed"].append({"pnl": pnl})

        return cash, position, available_position, today_bought, avg_cost, realized
