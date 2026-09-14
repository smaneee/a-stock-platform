"""组合回测引擎。

按交易日对齐多标的行情，逐日推进：上一日信号在本日开盘价成交（禁止未来数据），
实现 T+1、整数手、涨跌停/停牌约束（复用 ExecutionSimulator 与 MarketRuleEngine），
并落实单标的与总仓位限制。输出资产曲线、基准曲线与组合级绩效指标。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.backtest import metrics as single
from app.backtest.execution import ExecutionSimulator, capacity_multiple
from app.market_data.base import QuoteData
from app.market_rules.rules import MarketRuleEngine
from app.portfolio import benchmarks
from app.portfolio import metrics as port
from app.portfolio.config import PortfolioConfig
from app.portfolio.sentiment import (
    MISSING_DATA_REASON,
    GateDecision,
    SentimentGate,
)
from app.strategies.base import Signal, Strategy


@dataclass
class PortfolioResult:
    """组合回测结果。"""

    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    # 尾部风险（D9）
    volatility: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    max_drawdown_duration: int = 0
    worst_day_return: float = 0.0
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
    # D7 基准分列：同池等权（排序能力）与含现金（绝对收益）
    equal_weight_curve: list[float] = field(default_factory=list)
    cash_curve: list[float] = field(default_factory=list)
    equal_weight_return: float = 0.0
    cash_return: float = 0.0
    excess_vs_equal_weight: float = 0.0
    excess_vs_cash: float = 0.0
    benchmark_labels: dict = field(default_factory=dict)
    #: D5 容量：全部成交里最小的「容量倍数」（<1 表示超出容量）；未启用参与率限制时为 None
    min_capacity_multiple: float | None = None
    partial_fill_count: int = 0
    dates: list[str] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    sentiment: dict | None = None
    #: 本次实际使用的日线复权口径（D6），进 meta 一起返回
    bars_adjust: str = "none"
    #: D5 容量→规模换算：不超容量的账户规模上限（未启用参与率上限时为 None）
    capacity: dict | None = None

    def to_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "volatility": self.volatility,
            "var_95": self.var_95,
            "cvar_95": self.cvar_95,
            "max_drawdown_duration": self.max_drawdown_duration,
            "worst_day_return": self.worst_day_return,
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
            "equal_weight_curve": self.equal_weight_curve,
            "cash_curve": self.cash_curve,
            "equal_weight_return": self.equal_weight_return,
            "cash_return": self.cash_return,
            "excess_vs_equal_weight": self.excess_vs_equal_weight,
            "excess_vs_cash": self.excess_vs_cash,
            "benchmark_labels": self.benchmark_labels,
            "min_capacity_multiple": self.min_capacity_multiple,
            "partial_fill_count": self.partial_fill_count,
            "capacity": self.capacity,
            "dates": self.dates,
            "trades": self.trades,
            "sentiment": self.sentiment,
            # 收益口径标注（D8/D6）：必须随结果返回，避免把价格收益误读成总收益，
            # 或把前复权结果误读成未复权
            **single.return_convention_meta(self.bars_adjust),
        }


def sustainable_aum(
    trades: list[dict],
    dates: list,
    equity_curve: list[float],
) -> dict | None:
    """按已实现成交反推「不超容量」的账户规模上限（D5 的容量→AUM 换算）。

    推导
    ----
    某笔成交发生在账户净值 ``E``（成交日净值）下，委托 ``q`` 股；参与率上限下
    当日可成交 ``cap = volume × rate`` 股，``capacity_multiple = cap / q``。
    把账户整体放大 ``s`` 倍时，同权重、同价格下委托量变为 ``s·q``，于是要求
    ``s·q ≤ cap``，即 ``s ≤ capacity_multiple``。逐笔算出上限 ``E × multiple``，
    取**最小**者 —— 最紧的那一笔决定规模上限。

    为什么用「成交日净值」而不是初始资金
    ------------------------------------
    ``q`` 是由**当日**账户规模与目标权重推出来的，用初始资金当基数会在盈利/
    亏损后系统性高估或低估上限。

    这是**上界**，不是承诺
    ----------------------
    * 假设放大规模不改变价格与成交结构（**无市场冲击**）——真实情况下更大的
      委托本身会推动价格；
    * 只统计**已实现**的成交；整笔被拒的委托不在 ``trades`` 里，真实上限可能更低；
    * 参与率上限是唯一约束，未考虑涨跌停、停牌对可成交量的进一步限制。

    未启用参与率上限（``max_participation_rate=0``）时所有成交的
    ``capacity_multiple`` 都是 None → 返回 ``None``，调用方必须标注「未建模」，
    不得当成「容量无限」。
    """
    if not trades:
        return None
    equity_by_day = {day: value for day, value in zip(dates, equity_curve)}
    best: dict | None = None
    considered = 0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        multiple = trade.get("capacity_multiple")
        if multiple is None or multiple <= 0:
            continue
        try:
            day = datetime.fromisoformat(str(trade.get("time"))).date()
        except (TypeError, ValueError):
            continue
        equity = equity_by_day.get(day)
        if equity is None or equity <= 0:
            continue
        considered += 1
        candidate = equity * multiple
        if best is None or candidate < best["max_aum"]:
            best = {
                "max_aum": round(candidate, 2),
                "binding_trade": {
                    "date": day.isoformat(),
                    "symbol": trade.get("symbol"),
                    "side": trade.get("side"),
                    "quantity": trade.get("quantity"),
                    # 保留完整精度：max_aum 由它推出，取整会让两者对不上（展示层再取整）
                    "capacity_multiple": float(multiple),
                    "account_equity_on_that_day": float(equity),
                },
            }
    if best is None:
        return None
    best["basis"] = "min(account_equity_on_trade_day × capacity_multiple)"
    best["considered_trades"] = considered
    best["assumptions"] = [
        "无市场冲击假设：放大规模不改变价格与成交结构，因此这是上界而非承诺。",
        "只统计已实现的成交；整笔被拒的委托不在成交列表里，真实上限可能更低。",
        "只考虑成交量参与率上限，未叠加涨跌停/停牌对可成交量的进一步限制。",
    ]
    return best


class PortfolioBacktestEngine:
    """多标的组合回测引擎。"""

    def __init__(
        self,
        strategies: dict[str, Strategy],
        weights: dict[str, float] | None = None,
        config: PortfolioConfig | None = None,
        benchmark: list[QuoteData] | None = None,
        *,
        sentiment: SentimentGate | None = None,
    ):
        """
        strategies: symbol -> Strategy。weights 为可选目标权重（归一化后使用），
        缺省按等权。benchmark 为基准指数历史行情（可选）。

        sentiment 为市场情绪闸门（可选）。提供时只在信号产生日拦截新开仓（BUY），
        并把仓位系数带到次日成交；SELL 始终放行，用于弱势市况退出。
        """
        self.strategies = strategies
        self.config = config or PortfolioConfig()
        self.benchmark = self._sort(benchmark or [])
        self.sentiment = sentiment
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
        # 值为 (信号, 仓位系数)：系数在信号日由情绪闸门决定，次日成交时生效
        pending: dict[str, tuple[Signal, float]] = {}
        sentiment_days: list[dict] = []
        blocked_buy_signals = 0
        buy_signals = 0
        exposure_total = 0.0

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
            for symbol, (signal, exposure) in list(pending.items()):
                bar = bars_today.get(symbol)
                if bar is None:
                    continue
                current_equity = self._current_equity(
                    cash, position, bars_today, last_close
                )
                cash, position, available, today_bought, avg_cost, executed = self._execute(
                    signal,
                    bar,
                    current_equity,
                    cash,
                    position,
                    available,
                    today_bought,
                    avg_cost,
                    exposure=exposure,
                )
                trades.extend(executed["trades"])
                closed_trades.extend(executed["closed"])
                if executed["traded_value"]:
                    traded_values.append(executed["traded_value"])
            pending.clear()

            # 市场情绪闸门：只影响"今天是否开新仓"以及新仓的仓位系数
            decision = self._sentiment_decision(date)
            gate_blocked = decision is not None and not decision.allowed
            gate_exposure = decision.exposure if decision is not None else 1.0
            if decision is not None:
                sentiment_days.append(decision.to_dict())

            # 2) 用截至本日的数据生成新信号（收盘后产生，次日成交）
            for symbol in symbols:
                bar = bars_today.get(symbol)
                if bar is None:
                    continue
                hist = [b for b in bars[symbol] if (b.market_time or b.received_at).date() <= date]
                sig = self.strategies[symbol].analyze(hist)
                if sig is None:
                    continue
                if sig.direction == "BUY" and gate_blocked:
                    blocked_buy_signals += 1
                    continue
                if sig.direction == "BUY":
                    buy_signals += 1
                    exposure_total += gate_exposure
                    pending[symbol] = (sig, gate_exposure)
                else:
                    pending[symbol] = (sig, 1.0)

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

            # 4) 基准估值（归一化到初始现金）。未提供基准时不产出曲线：
            #    全 0 的「假基准」会让超额收益 / 跟踪误差 / 信息比率全部失真。
            if self.benchmark:
                benchmark_curve.append(
                    bench_index.value_at(date, bench_start, self.config.initial_cash)
                )

            previous_date = date

        result = self._finalize(
            symbols=symbols,
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            holdings_history=holdings_history,
            traded_values=traded_values,
            trades=trades,
            closed_trades=closed_trades,
            dates=dates,
            # D7 同池等权基准：完全相同的标的池与日期区间，衡量池内排序能力
            equal_weight_curve=benchmarks.equal_weight_buy_and_hold(
                {
                    symbol: {
                        (bar.market_time or bar.received_at).date(): float(bar.price)
                        for bar in hist
                        if bar.price and bar.price > 0
                    }
                    for symbol, hist in bars.items()
                },
                list(dates),
                self.config.initial_cash,
            ),
        )
        result.sentiment = self._sentiment_summary(
            sentiment_days, blocked_buy_signals, buy_signals, exposure_total
        )
        return result

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
        exposure: float = 1.0,
    ) -> tuple[float, dict, dict, dict, dict, dict]:
        """执行单只标的信号，返回更新后状态与成交记录。"""
        symbol = signal.symbol
        executed: dict = {"trades": [], "closed": [], "traded_value": 0.0}

        if signal.direction == "BUY":
            result = self._buy(signal, bar, current_equity, cash, exposure)
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
            "transfer_fee": result.get("transfer_fee", 0.0),
            "pnl": result.get("pnl", 0.0),
            # D5 容量与部分成交：由 _buy/_sell 在成交时算好并透传
            "capacity_multiple": result.get("capacity_multiple"),
            "partial": bool(result.get("partial", False)),
            "requested_quantity": result.get("requested_quantity") or qty,
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
        exposure: float = 1.0,
    ) -> dict | None:
        """按目标权重买入，落实单标的与总仓位限制。"""
        symbol = signal.symbol
        target_weight = self.weights.get(symbol, 0.0)
        # 情绪闸门的仓位系数：情绪弱时按比例缩量，但不放宽单票 / 总仓位上限
        target_value = current_equity * target_weight * max(0.0, min(1.0, exposure))
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
            transfer_fee = value * self.simulator.config.transfer_fee_rate
            if value + commission + transfer_fee <= cash:
                break
            quantity -= 100
        if quantity < 100:
            return None

        fill = self.simulator.try_fill("BUY", quantity, bar)
        if not fill.filled:
            return None
        return {
            "cash": cash - (fill.price * fill.quantity + fill.commission + fill.transfer_fee),
            "price": fill.price,
            "quantity": fill.quantity,
            "commission": fill.commission,
            "transfer_fee": fill.transfer_fee,
            # D5 容量：本笔需求相对「参与率上限下可成交量」的倍数（未启用时为 None）
            "capacity_multiple": capacity_multiple(
                fill.requested_quantity or fill.quantity,
                fill.price,
                bar.volume,
                self.simulator.config.max_participation_rate,
            ),
            "partial": fill.partial,
            "requested_quantity": fill.requested_quantity or fill.quantity,
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
        proceeds = (
            fill.price * fill.quantity
            - fill.commission
            - fill.stamp_tax
            - fill.transfer_fee
        )
        pnl = (
            (fill.price - avg_cost[symbol]) * fill.quantity
            - fill.commission
            - fill.stamp_tax
            - fill.transfer_fee
        )
        return {
            "cash": cash + proceeds,
            "price": fill.price,
            "quantity": fill.quantity,
            "commission": fill.commission,
            "stamp_tax": fill.stamp_tax,
            "transfer_fee": fill.transfer_fee,
            "pnl": pnl,
            "capacity_multiple": capacity_multiple(
                fill.requested_quantity or fill.quantity,
                fill.price,
                bar.volume,
                self.simulator.config.max_participation_rate,
            ),
            "partial": fill.partial,
            "requested_quantity": fill.requested_quantity or fill.quantity,
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

    def _sentiment_decision(self, trade_date) -> GateDecision | None:
        """该信号日的情绪闸门判定；未启用闸门时返回 None。"""
        if self.sentiment is None:
            return None
        return self.sentiment.decision(trade_date)

    def _sentiment_summary(
        self,
        days: list[dict],
        blocked_buy_signals: int,
        buy_signals: int,
        exposure_total: float,
    ) -> dict | None:
        """汇总情绪闸门在本轮回测里的作用，便于前端复盘与核对参数。"""
        if self.sentiment is None:
            return None
        allowed = [item for item in days if item["allowed"]]
        missing = [
            item["date"] for item in days if MISSING_DATA_REASON in item["reasons"]
        ]
        return {
            "config": self.sentiment.config.to_dict(),
            "series_days": len(self.sentiment),
            "total_days": len(days),
            "allowed_days": len(allowed),
            "blocked_days": len(days) - len(allowed),
            "missing_days": missing,
            "buy_signals": buy_signals,
            "blocked_buy_signals": blocked_buy_signals,
            "average_buy_exposure": (
                exposure_total / buy_signals if buy_signals else 0.0
            ),
            "days": days,
        }

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
                return float(initial_cash)
            # 取当日或最近一个交易日收盘价
            bar = self._by_date.get(date)
            price = bar.price if bar else self._last_close_on_or_before(date)
            if not price or price <= 0:
                # 基准在该日之前还没有数据：按初始资金持平，不能记 0
                return float(initial_cash)
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
        equal_weight_curve: list[float] | None = None,
    ) -> PortfolioResult:
        result = PortfolioResult(
            equity_curve=equity_curve,
            trades=trades,
            bars_adjust=self.config.bars_adjust,
        )
        result.trade_count = len(trades)
        result.dates = [d.isoformat() for d in dates]

        # D7 基准分列：同池等权 + 含现金，始终产出（不依赖是否配置市场指数）
        ew_curve = equal_weight_curve or []
        result.equal_weight_curve = ew_curve
        result.cash_curve = benchmarks.cash_curve(list(dates), self.config.initial_cash)
        result.equal_weight_return = benchmarks.return_of(ew_curve, self.config.initial_cash)
        result.cash_return = 0.0
        result.excess_vs_cash = benchmarks.return_of(equity_curve, self.config.initial_cash)
        result.excess_vs_equal_weight = result.excess_vs_cash - result.equal_weight_return
        result.benchmark_labels = benchmarks.benchmark_labels(bool(self.benchmark))

        # D5 容量：把每笔成交的容量倍数汇总成「最小倍数」（<1 即超出容量）
        multiples = [
            trade["capacity_multiple"]
            for trade in trades
            if isinstance(trade, dict) and trade.get("capacity_multiple") is not None
        ]
        result.min_capacity_multiple = min(multiples) if multiples else None
        result.partial_fill_count = sum(
            1 for trade in trades if isinstance(trade, dict) and trade.get("partial")
        )
        # D5 容量→规模换算：不超容量的账户规模上界（未启用参与率上限时为 None）
        result.capacity = sustainable_aum(trades, dates, equity_curve)

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
        # 尾部风险（D9）：全部基于净值序列本身，可独立复核
        result.volatility = single.volatility(equity_curve)
        result.var_95 = single.value_at_risk(equity_curve, 0.95)
        result.cvar_95 = single.conditional_value_at_risk(equity_curve, 0.95)
        result.max_drawdown_duration = single.max_drawdown_duration(equity_curve)
        result.worst_day_return = single.worst_day_return(equity_curve)
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
