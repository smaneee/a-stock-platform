"""组合回测引擎。

按交易日对齐多标的行情，逐日推进：上一日信号在本日开盘价成交（禁止未来数据），
实现 T+1、整数手、涨跌停/停牌约束（复用 ExecutionSimulator 与 MarketRuleEngine），
并落实单标的与总仓位限制。输出资产曲线、基准曲线与组合级绩效指标。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.backtest import metrics as single
from app.backtest.execution import ExecutionSimulator
from app.market_data.base import QuoteData
from app.market_rules.rules import MarketRuleEngine
from app.portfolio import metrics as port
from app.portfolio.config import PortfolioConfig
from app.strategies.base import Signal, Strategy


@dataclass
class PortfolioResult:
    """组合回测结果。"""

    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    concentration: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    benchmark_curve: list[float] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
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
            "turnover": self.turnover,
            "concentration": self.concentration,
            "benchmark_return": self.benchmark_return,
            "excess_return": self.excess_return,
            "alpha": self.alpha,
            "beta": self.beta,
            "information_ratio": self.information_ratio,
            "tracking_error": self.tracking_error,
            "equity_curve": self.equity_curve,
            "benchmark_curve": self.benchmark_curve,
            "dates": self.dates,
            "trades": self.trades,
        }


class PortfolioBacktestEngine:
    """多标的组合回测引擎。"""

    def __init__(
        self,
        strategies: dict[str, Strategy],
        weights: dict[str, float] | None = None,
        config: PortfolioConfig | None = None,
        benchmark: list[QuoteData] | None = None,
    ):
        """
        strategies: symbol -> Strategy。weights 为可选目标权重（归一化后使用），
        缺省按等权。benchmark 为基准指数历史行情（可选）。
        """
        self.strategies = strategies
        self.config = config or PortfolioConfig()
        self.benchmark = self._sort(benchmark or [])
        self.simulator = ExecutionSimulator(
            config=self.config.execution, rule_engine=MarketRuleEngine()
        )
        self.weights = self._normalize_weights(weights, list(strategies.keys()))

    @staticmethod
    def _sort(history: list[QuoteData]) -> list[QuoteData]:
        return sorted(history, key=lambda b: b.market_time or b.received_at)

    @staticmethod
    def _normalize_weights(
        weights: dict[str, float] | None, symbols: list[str]
    ) -> dict[str, float]:
        """归一化目标权重，缺省等权；未知标的忽略。"""
        if not weights:
            n = len(symbols) or 1
            return {s: 1.0 / n for s in symbols}
        known = {s: float(w) for s, w in weights.items() if s in symbols and w > 0}
        if not known:
            n = len(symbols) or 1
            return {s: 1.0 / n for s in symbols}
        total = sum(known.values())
        return {s: w / total for s, w in known.items()}

    def run(self, histories: dict[str, list[QuoteData]]) -> PortfolioResult:
        """运行组合回测。histories: symbol -> 历史 K 线。"""
        symbols = [s for s in self.strategies if s in histories and histories[s]]
        if not symbols:
            return PortfolioResult()

        bars = {s: self._sort(histories[s]) for s in symbols}
        dates = self._date_axis(bars)

        cash = self.config.initial_cash
        position: dict[str, int] = {s: 0 for s in symbols}
        available: dict[str, int] = {s: 0 for s in symbols}
        today_bought: dict[str, int] = {s: 0 for s in symbols}
        avg_cost: dict[str, float] = {s: 0.0 for s in symbols}
        last_close: dict[str, float] = {s: 0.0 for s in symbols}
        pending: dict[str, Signal] = {}

        equity_curve: list[float] = []
        benchmark_curve: list[float] = []
        holdings_history: list[list[float]] = []
        traded_values: list[float] = []
        trades: list[dict] = []
        closed_trades: list[dict] = []
        previous_date = None

        bench_index = self._BenchmarkIndex(self.benchmark)
        bench_start = bench_index.first_close

        for date in dates:
            bars_today = self._bars_on_date(bars, date)

            # T+1：换日解冻当日买入
            if previous_date is not None and date != previous_date:
                for s in symbols:
                    available[s] += today_bought[s]
                    today_bought[s] = 0

            # 1) 执行上一日产生的 pending 信号（本日开盘价成交）
            for symbol, signal in list(pending.items()):
                bar = bars_today.get(symbol)
                if bar is None:
                    continue
                current_equity = self._current_equity(
                    cash, position, bars_today, last_close
                )
                cash, position, available, today_bought, avg_cost, executed = self._execute(
                    signal, bar, current_equity, cash, position, available, today_bought, avg_cost
                )
                trades.extend(executed["trades"])
                closed_trades.extend(executed["closed"])
                if executed["traded_value"]:
                    traded_values.append(executed["traded_value"])
            pending.clear()

            # 2) 用截至本日的数据生成新信号（收盘后产生，次日成交）
            for symbol in symbols:
                bar = bars_today.get(symbol)
                if bar is None:
                    continue
                hist = [b for b in bars[symbol] if (b.market_time or b.received_at).date() <= date]
                sig = self.strategies[symbol].analyze(hist)
                if sig is not None:
                    pending[symbol] = sig

            # 3) 按收盘价估值
            total = cash
            holdings: list[float] = []
            for symbol in symbols:
                bar = bars_today.get(symbol)
                price = bar.price if bar else last_close[symbol]
                if price:
                    last_close[symbol] = price
                value = position[symbol] * price
                total += value
                holdings.append(value)
            equity_curve.append(total)
            holdings_history.append(holdings)

            # 4) 基准估值（归一化到初始现金）
            benchmark_curve.append(bench_index.value_at(date, bench_start, self.config.initial_cash))

            previous_date = date

        return self._finalize(
            symbols=symbols,
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            holdings_history=holdings_history,
            traded_values=traded_values,
            trades=trades,
            closed_trades=closed_trades,
            dates=dates,
        )

    # ──────── 执行 ────────

    @staticmethod
    def _current_equity(
        cash: float,
        position: dict[str, int],
        bars_today: dict[str, QuoteData],
        last_close: dict[str, float],
    ) -> float:
        """当前总资产 = 现金 + 各标的持仓市值（今日开盘价，缺省用昨日收盘）。"""
        total = cash
        for symbol, qty in position.items():
            bar = bars_today.get(symbol)
            price = bar.open if bar and bar.open > 0 else last_close.get(symbol, 0.0)
            total += qty * price
        return total

    def _execute(
        self,
        signal: Signal,
        bar: QuoteData,
        current_equity: float,
        cash: float,
        position: dict[str, int],
        available: dict[str, int],
        today_bought: dict[str, int],
        avg_cost: dict[str, float],
    ) -> tuple[float, dict, dict, dict, dict, dict]:
        """执行单只标的信号，返回更新后状态与成交记录。"""
        symbol = signal.symbol
        executed: dict = {"trades": [], "closed": [], "traded_value": 0.0}

        if signal.direction == "BUY":
            result = self._buy(signal, bar, current_equity, cash)
        elif signal.direction == "SELL":
            result = self._sell(signal, bar, cash, position, available, avg_cost)
        else:
            return cash, position, available, today_bought, avg_cost, executed

        if result is None:
            return cash, position, available, today_bought, avg_cost, executed

        cash = result["cash"]
        qty = result["quantity"]
        if signal.direction == "BUY":
            position[symbol] += qty
            today_bought[symbol] += qty
            new_qty = position[symbol]
            cost = result["price"] * qty + result["commission"]
            avg_cost[symbol] = (
                (avg_cost[symbol] * (new_qty - qty) + cost) / new_qty if new_qty else 0.0
            )
        else:
            position[symbol] -= qty
            available[symbol] -= qty
            if position[symbol] == 0:
                avg_cost[symbol] = 0.0

        executed["traded_value"] = result["price"] * qty
        trade = {
            "time": (bar.market_time or bar.received_at).isoformat(),
            "symbol": symbol,
            "side": signal.direction,
            "price": result["price"],
            "quantity": qty,
            "commission": result["commission"],
            "stamp_tax": result.get("stamp_tax", 0.0),
            "pnl": result.get("pnl", 0.0),
        }
        executed["trades"].append(trade)
        if signal.direction == "SELL":
            executed["closed"].append({"pnl": result.get("pnl", 0.0)})
        return cash, position, available, today_bought, avg_cost, executed

    def _buy(
        self,
        signal: Signal,
        bar: QuoteData,
        current_equity: float,
        cash: float,
    ) -> dict | None:
        """按目标权重买入，落实单标的与总仓位限制。"""
        symbol = signal.symbol
        target_weight = self.weights.get(symbol, 0.0)
        target_value = current_equity * target_weight
        # 单标的仓位上限
        target_value = min(target_value, current_equity * self.config.max_single_position)

        # 总仓位上限：本次买入后总市值不得超过上限
        current_market_value = current_equity - cash
        max_total_value = current_equity * self.config.max_total_position
        available_budget = max(0.0, max_total_value - current_market_value)

        buy_value = min(max(0.0, target_value), cash, available_budget)

        estimated_price = bar.open * (1 + self.simulator.config.slippage)
        if estimated_price <= 0:
            return None

        # 目标数量（100 股整数手），同时考虑佣金
        quantity = int(buy_value // estimated_price // 100) * 100
        while quantity >= 100:
            value = estimated_price * quantity
            commission = max(
                value * self.simulator.config.commission_rate,
                self.simulator.config.min_commission,
            )
            if value + commission <= cash:
                break
            quantity -= 100
        if quantity < 100:
            return None

        fill = self.simulator.try_fill("BUY", quantity, bar)
        if not fill.filled:
            return None
        return {
            "cash": cash - (fill.price * fill.quantity + fill.commission),
            "price": fill.price,
            "quantity": fill.quantity,
            "commission": fill.commission,
        }

    def _sell(
        self,
        signal: Signal,
        bar: QuoteData,
        cash: float,
        position: dict[str, int],
        available: dict[str, int],
        avg_cost: dict[str, float],
    ) -> dict | None:
        """全部卖出可用持仓。"""
        symbol = signal.symbol
        sell_qty = (available[symbol] // 100) * 100
        if sell_qty < 100:
            return None
        fill = self.simulator.try_fill("SELL", sell_qty, bar)
        if not fill.filled:
            return None
        proceeds = fill.price * fill.quantity - fill.commission - fill.stamp_tax
        pnl = (
            (fill.price - avg_cost[symbol]) * fill.quantity
            - fill.commission
            - fill.stamp_tax
        )
        return {
            "cash": cash + proceeds,
            "price": fill.price,
            "quantity": fill.quantity,
            "commission": fill.commission,
            "stamp_tax": fill.stamp_tax,
            "pnl": pnl,
        }

    # ──────── 日期对齐 ────────

    @staticmethod
    def _date_axis(bars: dict[str, list[QuoteData]]) -> list:
        dates = set()
        for hist in bars.values():
            for b in hist:
                dates.add((b.market_time or b.received_at).date())
        return sorted(dates)

    @staticmethod
    def _bars_on_date(bars: dict[str, list[QuoteData]], date) -> dict[str, QuoteData]:
        """返回每个标的本日的 K 线（无则缺省）。"""
        result = {}
        for symbol, hist in bars.items():
            for b in hist:
                if (b.market_time or b.received_at).date() == date:
                    result[symbol] = b
                    break
        return result

    # ──────── 基准索引 ────────

    class _BenchmarkIndex:
        def __init__(self, history: list[QuoteData]):
            self._by_date = {
                (b.market_time or b.received_at).date(): b for b in history
            }
            self._dates = sorted(self._by_date)
            self.first_close = history[0].price if history else 0.0

        def value_at(self, date, first_close, initial_cash) -> float:
            if not self._by_date or first_close <= 0:
                return 0.0
            # 取当日或最近一个交易日收盘价
            bar = self._by_date.get(date)
            price = bar.price if bar else self._last_close_on_or_before(date)
            return price / first_close * initial_cash

        def _last_close_on_or_before(self, date):
            prev = None
            for d in self._dates:
                if d <= date:
                    prev = d
                else:
                    break
            return self._by_date[prev].price if prev is not None else 0.0

    # ──────── 结果汇总 ────────

    def _finalize(
        self,
        symbols: list[str],
        equity_curve: list[float],
        benchmark_curve: list[float],
        holdings_history: list[list[float]],
        traded_values: list[float],
        trades: list[dict],
        closed_trades: list[dict],
        dates: list,
    ) -> PortfolioResult:
        result = PortfolioResult(equity_curve=equity_curve, trades=trades)
        result.trade_count = len(trades)
        result.dates = [d.isoformat() for d in dates]

        if not equity_curve:
            return result

        final_asset = equity_curve[-1]
        trading_days = len(equity_curve)
        result.total_return = single.total_return(final_asset, self.config.initial_cash)
        result.annual_return = single.annual_return(
            final_asset, self.config.initial_cash, trading_days
        )
        result.max_drawdown = single.max_drawdown(equity_curve)
        result.sharpe_ratio = single.sharpe_ratio(equity_curve, self.config.risk_free_rate)
        result.win_rate = single.win_rate(closed_trades)
        result.profit_loss_ratio = single.profit_loss_ratio(closed_trades)
        result.turnover = port.turnover(traded_values, equity_curve)

        # 集中度：每期持仓权重 HHI
        weights_history: list[list[float]] = []
        for i, total in enumerate(equity_curve):
            if total <= 0:
                weights_history.append([0.0] * len(symbols))
                continue
            weights_history.append([h / total for h in holdings_history[i]])
        result.concentration = port.concentration(weights_history)

        # 相对基准
        if benchmark_curve and len(benchmark_curve) == len(equity_curve):
            result.benchmark_curve = benchmark_curve
            bench_final = benchmark_curve[-1]
            if bench_final > 0:
                result.benchmark_return = (
                    bench_final - self.config.initial_cash
                ) / self.config.initial_cash
            result.excess_return = port.excess_return(
                result.total_return, result.benchmark_return
            )
            pr = port.daily_returns(equity_curve)
            br = port.daily_returns(benchmark_curve)
            result.beta = port.beta(pr, br)
            result.alpha = port.alpha(pr, br, self.config.risk_free_rate)
            result.information_ratio = port.information_ratio(pr, br)
            result.tracking_error = port.tracking_error(pr, br)

        return result
