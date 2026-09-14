"""P0-05 回测执行真实性回归测试。

覆盖《研发计划-2026Q4》5.3.5 / 7.6 / 12.3 的可验证条款，逐项给出实测证据：

1. 账户净值 = 现金 + 持仓市值逐日重估；现金守恒逐笔可核对；
2. 重叠持仓不得把多日持有收益连乘成净值；
3. T+1：当日买入不可卖（跨日解冻后才可卖）；
4. 停牌（成交量 0 / 当日无 K 线）不可成交；
5. 涨跌停：一字板不可买/不可卖，盘中开板可成交，触板未封不机械等同全天不可成交；
6. 成本：最低佣金、印花税（仅卖出单边）、过户费（双边）、滑点方向与上限；
7. 复权口径：回测走未复权（none）序列，涨跌停用同一未复权昨收，不混用前复权；
8. 基准：未提供基准时不得产出「全 0 假基准」与伪造的相对指标；
9. 重复请求幂等（已有覆盖见 tests/test_phase2_fix_m3.py::test_...idempotency）。

口径声明：本文件只用 mock 数据与内存 SQLite，不触碰 backend/a_stock.db，
不构成投资建议。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.execution import ExecutionConfig, ExecutionSimulator
from app.database.models import HistoricalBar, PortfolioBacktest
from app.history.quality import QualityReport
from app.history.service import ADJUST_NONE, HistoricalDataService, HistoryResult
from app.market_data.base import QuoteData
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine
from app.strategies.base import Signal, Strategy
from app.tasks.portfolio_worker import PortfolioBacktestWorker
from app.tasks.status import RUNNING, SUCCEEDED
from app.time_utils import utc_now

DAY0 = date(2026, 1, 5)
INITIAL_CASH = 1_000_000.0


# ──────────────── 构造工具 ────────────────


def _bar(
    symbol: str,
    day: date,
    close: float,
    *,
    open_: float | None = None,
    prev: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
) -> QuoteData:
    """构造一根日线；prev=None 表示「昨收未知」（历史服务老口径）。"""
    open_ = close if open_ is None else open_
    high = max(close, open_) if high is None else high
    low = min(close, open_) if low is None else low
    return QuoteData(
        symbol=symbol,
        name=f"测试{symbol}",
        price=close,
        open=open_,
        high=high,
        low=low,
        previous_close=0.0 if prev is None else prev,
        volume=volume,
        amount=10_000_000.0,
        bid_price=close - 0.01,
        ask_price=close + 0.01,
        source="mock",
        market_time=datetime(day.year, day.month, day.day, 9, 30),
    )


def _series(symbol: str, n: int, base: float = 10.0, step: float = 0.1, skip: int | None = None):
    """线性日线序列；skip 指定的下标模拟停牌（不产生 K 线）。"""
    out = []
    for i in range(n):
        if skip is not None and i == skip:
            continue
        px = base + i * step
        out.append(_bar(symbol, DAY0 + timedelta(days=i), px, open_=px - 0.02, prev=px - 0.1))
    return out


class Schedule(Strategy):
    """按已见 K 线数量触发的确定性策略（与生产策略信号逻辑无关）。"""

    name = "schedule"

    def __init__(self, buy_at: int = 2, sell_at: int = 99):
        self.buy_at = buy_at
        self.sell_at = sell_at

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if not history:
            return None
        n = len(history)
        if n == self.buy_at:
            direction = "BUY"
        elif n == self.sell_at:
            direction = "SELL"
        else:
            return None
        latest = history[-1]
        return Signal(
            symbol=latest.symbol,
            strategy_name=self.name,
            direction=direction,
            reason="测试信号",
            price=latest.price,
            source_time=latest.market_time,
        )


def _sim(**kwargs) -> ExecutionSimulator:
    return ExecutionSimulator(ExecutionConfig(slippage=0.0, **kwargs))


# ──────────────── 1. 现金守恒与估值一致性 ────────────────


def test_cash_conservation_and_equity_equals_cash_plus_market_value():
    """期末净值 = 现金 + 持仓市值；现金可按逐笔成交流水完全核对。"""
    bars_a = _series("600000", 8, 10.0)
    bars_b = _series("000001", 8, 20.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=5), "000001": Schedule(buy_at=3)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH, max_single_position=0.5),
    )
    res = engine.run({"600000": bars_a, "000001": bars_b})
    assert res.trade_count == 3  # 600000 一买一卖 + 000001 一买

    # 逐笔核对现金（含佣金、印花税、过户费）
    cash = INITIAL_CASH
    qty = {"600000": 0, "000001": 0}
    for t in res.trades:
        fee = t["commission"] + t.get("transfer_fee", 0.0)
        if t["side"] == "BUY":
            cash -= t["price"] * t["quantity"] + fee
            qty[t["symbol"]] += t["quantity"]
        else:
            cash += t["price"] * t["quantity"] - fee - t["stamp_tax"]
            qty[t["symbol"]] -= t["quantity"]
        assert cash >= 0  # 现金不可透支

    last_close = {"600000": bars_a[-1].price, "000001": bars_b[-1].price}
    expected_final = cash + sum(qty[s] * last_close[s] for s in qty)
    assert res.equity_curve[-1] == pytest.approx(expected_final, rel=1e-12)
    assert res.equity_curve[0] == pytest.approx(INITIAL_CASH)
    assert res.total_return == pytest.approx(res.equity_curve[-1] / INITIAL_CASH - 1.0)


def test_overlapping_positions_are_marked_to_market_not_compounded():
    """两笔重叠持仓：逐日净值必须等于「现金 + 当日收盘市值」，不存在收益连乘。"""
    bars_a = _series("600000", 9, 10.0, step=0.2)
    bars_b = _series("000001", 9, 30.0, step=-0.2)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=8), "000001": Schedule(buy_at=4, sell_at=8)},
        config=PortfolioConfig(
            initial_cash=INITIAL_CASH, max_single_position=0.5, max_total_position=1.0
        ),
    )
    res = engine.run({"600000": bars_a, "000001": bars_b})

    # 独立复算（不复用引擎内部状态）
    closes = {
        "600000": {DAY0 + timedelta(days=i): b.price for i, b in enumerate(bars_a)},
        "000001": {DAY0 + timedelta(days=i): b.price for i, b in enumerate(bars_b)},
    }
    trades_by_day: dict[str, list[dict]] = {}
    for t in res.trades:
        trades_by_day.setdefault(t["time"][:10], []).append(t)

    cash = INITIAL_CASH
    qty = {"600000": 0, "000001": 0}
    curve, both_held_days = [], 0
    for offset, day in enumerate(sorted({*closes["600000"], *closes["000001"]})):
        for t in trades_by_day.get(day.isoformat(), []):
            fee = t["commission"] + t.get("transfer_fee", 0.0)
            if t["side"] == "BUY":
                cash -= t["price"] * t["quantity"] + fee
                qty[t["symbol"]] += t["quantity"]
            else:
                cash += t["price"] * t["quantity"] - fee - t["stamp_tax"]
                qty[t["symbol"]] -= t["quantity"]
        mv = 0.0
        for symbol, day_close in closes.items():
            price = day_close.get(day)
            if price is None:  # 停牌日按最近收盘估值
                price = max((d for d in day_close if d <= day), default=None)
                price = day_close[price] if price else 0.0
            mv += qty[symbol] * price
        if qty["600000"] > 0 and qty["000001"] > 0:
            both_held_days += 1
        curve.append(cash + mv)

    assert both_held_days > 0, "必须真的构造出重叠持仓"
    assert res.equity_curve == pytest.approx(curve, rel=1e-12)


# ──────────────── 2. T+1 ────────────────


def _signal(symbol: str, direction: str, bar: QuoteData) -> Signal:
    return Signal(
        symbol=symbol,
        strategy_name="schedule",
        direction=direction,
        reason="T+1 测试",
        price=bar.price,
        source_time=bar.market_time,
    )


def test_t1_blocks_same_day_sell_of_newly_bought_shares():
    """当日买入的股票当日不可卖；跨日解冻后才可卖。"""
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule()},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    bar = _bar("600000", DAY0, 10.0, prev=10.0)
    position = {"600000": 0}
    available = {"600000": 0}
    today_bought = {"600000": 0}
    avg_cost = {"600000": 0.0}

    cash, position, available, today_bought, avg_cost, executed = engine._execute(
        _signal("600000", "BUY", bar),
        bar,
        INITIAL_CASH,
        INITIAL_CASH,
        position,
        available,
        today_bought,
        avg_cost,
    )
    assert [t["side"] for t in executed["trades"]] == ["BUY"]
    bought = position["600000"]
    assert bought > 0
    assert available["600000"] == 0 and today_bought["600000"] == bought

    # 同日卖出必须被拦截
    bar_same_day = _bar("600000", DAY0, 10.5, prev=10.0)
    cash2, position2, available2, _, _, executed2 = engine._execute(
        _signal("600000", "SELL", bar_same_day),
        bar_same_day,
        cash,
        cash,
        position,
        available,
        today_bought,
        avg_cost,
    )
    assert executed2["trades"] == []
    assert position2["600000"] == bought
    assert cash2 == cash

    # 跨日解冻后可卖
    available["600000"] += today_bought["600000"]
    today_bought["600000"] = 0
    bar_next = _bar("600000", DAY0 + timedelta(days=1), 10.5, prev=10.0)
    _, position3, available3, _, _, executed3 = engine._execute(
        _signal("600000", "SELL", bar_next),
        bar_next,
        cash,
        cash,
        position,
        available,
        today_bought,
        avg_cost,
    )
    assert [t["side"] for t in executed3["trades"]] == ["SELL"]
    assert position3["600000"] == 0 and available3["600000"] == 0


def test_end_to_end_sell_never_lands_on_buy_day():
    """端到端：买入成交日的卖出信号只能顺延到下一交易日成交。"""
    bars = _series("600000", 8, 10.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=3)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    buy = next(t for t in res.trades if t["side"] == "BUY")
    sell = next(t for t in res.trades if t["side"] == "SELL")
    assert sell["time"][:10] > buy["time"][:10]
    assert sell["quantity"] == buy["quantity"]


# ──────────────── 3. 停牌 ────────────────


def test_suspended_symbol_is_valued_at_last_close_while_others_trade():
    """停牌日无 K 线：不成交，停牌标的按最近收盘估值（不归零、不跳变）。"""
    bars_a = _series("600000", 6, 10.0)
    bars_b = _series("000001", 6, 20.0, skip=3)  # 000001 第 4 天停牌
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=99), "000001": Schedule(buy_at=2)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH, max_single_position=0.5),
    )
    res = engine.run({"600000": bars_a, "000001": bars_b})

    assert res.dates[3] == (DAY0 + timedelta(days=3)).isoformat()  # 交易日轴由未停牌标的保留
    assert res.trade_count == 2  # 两只各买入一次
    # 000001 停牌当天不成交、不产生额外交易
    assert [t["symbol"] for t in res.trades if t["time"][:10] == res.dates[3]] == []
    # 独立复算：000001 用最近一根收盘（第 3 天）估值
    buy_b = next(t for t in res.trades if t["symbol"] == "000001")
    cash = INITIAL_CASH - sum(
        t["price"] * t["quantity"] + t["commission"] + t.get("transfer_fee", 0.0)
        for t in res.trades
    )
    qty_b = buy_b["quantity"]
    # 停牌日之前最后一根 K 线的收盘价（第 3 天停牌，故取到第 3 天为止的最后一条）
    prev_close_b = [
        b.price for b in bars_b if b.market_time.date() <= DAY0 + timedelta(days=2)
    ][-1]
    qty_a = next(t["quantity"] for t in res.trades if t["symbol"] == "600000")
    expected = cash + qty_b * prev_close_b + qty_a * bars_a[3].price
    assert res.equity_curve[3] == pytest.approx(expected, rel=1e-12)


def test_suspension_volume_zero_blocks_execution_in_engine():
    """执行日成交量为 0（停牌）时，挂起的买入信号不得成交。"""
    bars = _series("600000", 5, 10.0)
    bars[2] = _bar("600000", DAY0 + timedelta(days=2), 10.2, open_=10.0, prev=9.9, volume=0.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    assert [t for t in res.trades if t["side"] == "BUY"] == []


# ──────────────── 4. 涨跌停：一字板 vs 盘中开板 ────────────────


def test_one_word_limit_up_blocks_buy_and_limit_down_blocks_sell():
    """一字板（全天封死）不可买 / 不可卖。"""
    sim = _sim()
    up = _bar("600000", DAY0, 11.0, open_=11.0, prev=10.0, high=11.0, low=11.0)
    r = sim.try_fill("BUY", 100, up)
    assert r.filled is False and "涨停" in r.reason

    down = _bar("600000", DAY0, 9.0, open_=9.0, prev=10.0, high=9.0, low=9.0)
    r2 = sim.try_fill("SELL", 100, down)
    assert r2.filled is False and "跌停" in r2.reason

    # 涨停日不可买不等于不可卖：持筹可以在一字涨停卖出
    assert sim.try_fill("SELL", 100, up).filled is True
    # 一字跌停也不等于不可买
    assert sim.try_fill("BUY", 100, down).filled is True


def test_intraday_board_open_allows_buy_at_limit_price():
    """开盘即涨停但盘中开板（low < 涨停价）：不能机械判定为全天不可成交。"""
    sim = _sim()
    bar = _bar("600000", DAY0, 10.9, open_=11.0, prev=10.0, high=11.0, low=10.4)
    r = sim.try_fill("BUY", 100, bar)
    assert r.filled is True
    assert r.price == pytest.approx(11.0)  # 保守按涨停价成交，且不得高于涨停价


def test_intraday_board_open_allows_sell_at_limit_down_price():
    """开盘即跌停但盘中开板（high > 跌停价）：允许按跌停价卖出。"""
    sim = _sim()
    bar = _bar("600000", DAY0, 9.1, open_=9.0, prev=10.0, high=9.6, low=9.0)
    r = sim.try_fill("SELL", 100, bar)
    assert r.filled is True
    assert r.price == pytest.approx(9.0)


def test_touch_limit_without_sealing_fills_at_open():
    """盘中触及涨停但未封死：按开盘价成交，不得机械拒绝。"""
    sim = _sim()
    bar = _bar("600000", DAY0, 10.9, open_=10.2, prev=10.0, high=11.0, low=10.1)
    r = sim.try_fill("BUY", 100, bar)
    assert r.filled is True and r.price == pytest.approx(10.2)


def test_slippage_cannot_push_fill_price_through_price_limit():
    """成交价受涨跌停价约束：滑点不得把买入价推到涨停价之上。"""
    sim = ExecutionSimulator(ExecutionConfig(slippage=0.02))
    bar = _bar("600000", DAY0, 10.5, open_=10.95, prev=10.0, high=10.99, low=10.4)
    r = sim.try_fill("BUY", 100, bar)
    assert r.filled is True
    assert r.price == pytest.approx(11.0)  # 10.95 * 1.02 = 11.169 → 被涨停价截断

    bar_dn = _bar("000001", DAY0, 9.5, open_=9.05, prev=10.0, high=9.6, low=9.01)
    r2 = sim.try_fill("SELL", 100, bar_dn)
    assert r2.filled is True
    assert r2.price == pytest.approx(9.0)  # 9.05 * 0.98 = 8.869 → 被跌停价托住


def test_missing_previous_close_skips_limit_check_documented_gap():
    """特征化测试：昨收缺失时涨跌停约束被跳过（历史服务已补昨收，见下文口径测试）。

    本条固定「执行层依赖 bar.previous_close」这一事实：修复数据侧之后仍需保证
    该字段非零，否则约束会静默失效。
    """
    sim = _sim()
    bar = _bar("600000", DAY0, 11.0, open_=11.0, prev=None, high=11.0, low=11.0)
    assert sim.try_fill("BUY", 100, bar).filled is True


# ──────────────── 5. 成本：最低佣金 / 印花税 / 过户费 / 滑点 ────────────────


def test_min_commission_and_stamp_tax_single_side_hand_calc():
    """手算校验：小额按最低佣金 5 元；印花税仅卖出单边 0.05%；过户费双边 0.001%。"""
    sim = _sim(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0005)
    bar = _bar("600000", DAY0, 10.0, open_=10.0, prev=10.0)

    buy = sim.try_fill("BUY", 100, bar)  # 成交额 1,000 元
    assert buy.filled is True
    assert buy.commission == pytest.approx(5.0)  # 0.3 元 < 最低 5 元
    assert buy.stamp_tax == 0.0  # 买入不缴印花税
    assert buy.transfer_fee == pytest.approx(1000.0 * 0.00001)  # 双边过户费

    sell = sim.try_fill("SELL", 100, bar)
    assert sell.commission == pytest.approx(5.0)
    assert sell.stamp_tax == pytest.approx(1000.0 * 0.0005)
    assert sell.transfer_fee == pytest.approx(1000.0 * 0.00001)


def test_commission_above_minimum_scales_with_rate():
    """成交额较大时佣金按费率（而非最低值）计取。"""
    sim = _sim(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0005)
    bar = _bar("600000", DAY0, 10.0, open_=10.0, prev=10.0)
    big = sim.try_fill("BUY", 10_000, bar)  # 100,000 元
    assert big.commission == pytest.approx(100_000.0 * 0.0003)
    assert big.commission > 5.0

    sell = sim.try_fill("SELL", 10_000, bar)
    assert sell.stamp_tax == pytest.approx(100_000.0 * 0.0005)
    assert sell.transfer_fee == pytest.approx(100_000.0 * 0.00001)


def test_engine_trade_records_carry_all_costs():
    """引擎成交记录必须逐笔带上佣金/印花税/过户费，便于外部复核。"""
    bars = _series("600000", 6, 10.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=4)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    buy = next(t for t in res.trades if t["side"] == "BUY")
    sell = next(t for t in res.trades if t["side"] == "SELL")
    for field in ("commission", "stamp_tax", "transfer_fee"):
        assert field in buy and field in sell
    assert buy["stamp_tax"] == 0.0
    assert sell["stamp_tax"] > 0.0
    assert buy["transfer_fee"] > 0.0 and sell["transfer_fee"] > 0.0


def test_cost_sensitivity_doubling_rates_reduces_return_with_same_trades():
    """成本敏感性：佣金与滑点同时翻倍，成交序列不变、收益必须下降。"""
    from dataclasses import replace

    from app.portfolio.sensitivity import run_sensitivity

    bars = _series("600000", 20, 10.0, step=0.1)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=18)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    base = engine.run({"600000": bars})
    base_exec = engine.config.execution
    doubled = replace(
        engine.config,
        execution=replace(
            base_exec,
            commission_rate=base_exec.commission_rate * 2,
            slippage=base_exec.slippage * 2,
        ),
    )
    stressed = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=18)},
        config=doubled,
    ).run({"600000": bars})

    assert stressed.trade_count == base.trade_count
    assert stressed.total_return < base.total_return
    grid = run_sensitivity(
        engine,
        {"600000": bars},
        commission_rates=[0.0003, 0.003],
        slippages=[0.0005, 0.005],
    )
    assert len(grid) == 4
    by_key = {(round(r["commission_rate"], 6), round(r["slippage"], 6)): r for r in grid}
    assert by_key[(0.003, 0.0005)]["total_return"] < by_key[(0.0003, 0.0005)]["total_return"]
    assert by_key[(0.0003, 0.005)]["total_return"] < by_key[(0.0003, 0.0005)]["total_return"]


# ──────────────── 6. 复权口径：未复权序列 + 未复权涨跌停 ────────────────


def _seed_bar(db, symbol: str, day: str, close: float, open_: float, high: float, low: float):
    db.add(
        HistoricalBar(
            symbol=symbol,
            period="daily",
            adjust=ADJUST_NONE,
            trade_date=date.fromisoformat(day),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=1_000_000.0,
            amount=10_000_000.0,
            source="tdx",
            fetched_at=utc_now(),
        )
    )


def test_history_service_fills_unadjusted_previous_close(db_session):
    """历史服务必须补未复权昨收，否则执行层涨跌停约束静默失效。"""
    _seed_bar(db_session, "600000", "2026-01-05", 10.0, 9.9, 10.1, 9.8)
    _seed_bar(db_session, "600000", "2026-01-06", 11.0, 11.0, 11.0, 11.0)
    db_session.commit()

    service = HistoricalDataService(db_session)
    bars = service.get_cached(
        "600000", datetime(2026, 1, 1), datetime(2026, 1, 10), adjust=ADJUST_NONE
    )
    assert len(bars) == 2
    assert bars[0].previous_close == 0.0  # 区间首根没有更早数据
    assert bars[1].previous_close == pytest.approx(10.0)

    # 区间第二根是一字涨停（昨收 10 → 涨停 11）：必须拒绝买入
    sim = _sim()
    r = sim.try_fill("BUY", 100, bars[1])
    assert r.filled is False and "涨停" in r.reason


def test_limit_check_uses_unadjusted_previous_close_on_ex_dividend_day(db_session):
    """除权日口径：跌停按未复权昨收计算，不得用前复权价冒充昨收。"""
    _seed_bar(db_session, "600000", "2026-01-05", 20.0, 19.9, 20.1, 19.8)  # 除权前
    _seed_bar(db_session, "600000", "2026-01-06", 18.0, 18.0, 18.0, 18.0)  # 除权后一字跌停
    db_session.commit()

    bars = HistoricalDataService(db_session).get_cached(
        "600000", datetime(2026, 1, 1), datetime(2026, 1, 10), adjust=ADJUST_NONE
    )
    assert bars[1].previous_close == pytest.approx(20.0)  # 未复权昨收
    sim = _sim()
    # 未复权跌停价 = 20 * 0.9 = 18.00 → 一字跌停不可卖
    assert sim.try_fill("SELL", 100, bars[1]).filled is False


async def test_portfolio_worker_uses_qfq_with_unadjusted_limit_reference(
    db_session, monkeypatch
):
    """D6：策略/收益用前复权，同时另取未复权序列还原昨收（涨跌停判断）。

    原实现整条链路都用未复权，导致除权跳空被当成真实亏损；改造后默认前复权，
    但**涨跌停判断必须仍用未复权昨收**，因此 worker 要请求两个口径。
    """
    captured: list[dict] = []

    async def fake_get_history(self, symbol, start, end, **kwargs):
        captured.append({"symbol": symbol, **kwargs})
        bars = _series(symbol, 60, 10.0, step=0.01)
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("100000"),
        status=RUNNING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=__import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(
            bind=db_session.bind, autoflush=False, autocommit=False
        )
    )
    await worker._execute(task.id)

    adjusts = [call.get("adjust") for call in captured]
    assert "qfq" in adjusts, f"策略/收益口径必须是前复权，实际请求={adjusts}"
    assert "none" in adjusts, f"涨跌停昨收必须另取未复权序列，实际请求={adjusts}"


async def test_portfolio_worker_can_revert_to_unadjusted(db_session, monkeypatch):
    """一键回退：portfolio_bars_adjust=none 时只请求未复权（改造前行为）。"""
    captured: list[dict] = []

    async def fake_get_history(self, symbol, start, end, **kwargs):
        captured.append({"symbol": symbol, **kwargs})
        bars = _series(symbol, 60, 10.0, step=0.01)
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "portfolio_bars_adjust", "none", raising=False
    )
    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("100000"),
        status=RUNNING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=__import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(
            bind=db_session.bind, autoflush=False, autocommit=False
        )
    )
    await worker._execute(task.id)
    assert all(call.get("adjust") == "none" for call in captured)


# ──────────────── 7. worker 必须使用任务里的成本参数 ────────────────


async def test_worker_applies_task_commission_rate_and_slippage(db_session, monkeypatch):
    """API 提交的 commission_rate / slippage 必须真正进入执行配置。"""
    bars = _series("600000", 60, 10.0, step=0.05)

    async def fake_get_history(self, symbol, start, end, **kwargs):
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    monkeypatch.setattr(
        "app.strategies.registry.get_strategy", lambda name: Schedule(buy_at=2, sell_at=40)
    )

    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("1000000"),
        max_single_position=Decimal("0.2"),
        max_total_position=Decimal("0.95"),
        commission_rate=Decimal("0.0025"),
        slippage=Decimal("0.01"),
        status=RUNNING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from sqlalchemy.orm import sessionmaker

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    )
    await worker._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED

    payload = json.loads(task.result)
    buy = next(t for t in payload["trades"] if t["side"] == "BUY")
    # 成交日 K 线开盘价（信号在 n=2 产生，n=3 执行）
    exec_bar = bars[2]
    assert buy["price"] == pytest.approx(exec_bar.open * 1.01)  # 滑点 1% 生效
    assert buy["commission"] == pytest.approx(buy["price"] * buy["quantity"] * 0.0025)


async def test_worker_falls_back_to_default_cost_when_task_fields_null(
    db_session, monkeypatch
):
    """老任务行的成本字段为 NULL 时按默认系数执行，不得因 float(None) 直接失败。"""
    bars = _series("600000", 60, 10.0, step=0.05)

    async def fake_get_history(self, symbol, start, end, **kwargs):
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    monkeypatch.setattr(
        "app.strategies.registry.get_strategy", lambda name: Schedule(buy_at=2, sell_at=40)
    )

    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("1000000"),
        commission_rate=None,
        slippage=None,
        status=RUNNING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from sqlalchemy.orm import sessionmaker

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    )
    await worker._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED
    payload = json.loads(task.result)
    buy = next(t for t in payload["trades"] if t["side"] == "BUY")
    assert buy["price"] == pytest.approx(bars[2].open * 1.0005)  # 默认滑点 5bp


# ──────────────── 8. 基准分列与口径 ────────────────


def test_no_benchmark_must_not_fake_relative_metrics():
    """未提供基准：不得输出全 0 基准曲线，也不得伪造超额/跟踪误差/信息比率。"""
    bars = _series("600000", 12, 10.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=8)},
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    assert res.equity_curve and len(res.equity_curve) == len(res.dates)
    assert res.benchmark_curve == []
    assert res.benchmark_return == 0.0
    assert res.excess_return == 0.0
    assert res.beta == 0.0 and res.alpha == 0.0
    assert res.information_ratio == 0.0 and res.tracking_error == 0.0


def test_benchmark_curve_is_flat_until_first_benchmark_bar():
    """基准起点缺失时按初始资金持平，不能记 0（否则会出现 -100% 的假暴跌）。"""
    bars = _series("600000", 6, 10.0)
    bench = _series("000300", 6, 100.0, step=1.0)[2:]  # 基准第 3 天才有数据
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=5)},
        benchmark=bench,
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    assert res.benchmark_curve[0] == pytest.approx(INITIAL_CASH)
    assert res.benchmark_curve[1] == pytest.approx(INITIAL_CASH)
    assert res.benchmark_curve[2] == pytest.approx(INITIAL_CASH)
    assert res.benchmark_curve[-1] > INITIAL_CASH
    assert res.benchmark_return > 0


def test_benchmark_curve_normalized_to_initial_cash_and_same_length():
    bars = _series("600000", 8, 10.0)
    bench = _series("000300", 8, 100.0, step=1.0)
    engine = PortfolioBacktestEngine(
        strategies={"600000": Schedule(buy_at=2, sell_at=6)},
        benchmark=bench,
        config=PortfolioConfig(initial_cash=INITIAL_CASH),
    )
    res = engine.run({"600000": bars})
    assert len(res.benchmark_curve) == len(res.equity_curve) == len(res.dates)
    assert res.benchmark_curve[0] == pytest.approx(INITIAL_CASH)
    assert res.excess_return == pytest.approx(res.total_return - res.benchmark_return)


# ──────────────── 9. 容量与复权口径（D5 / D6） ────────────────


def test_capacity_limit_disabled_by_default_keeps_legacy_behavior():
    """D5 落地后：默认 `max_participation_rate=0` 仍是不限容量（向后兼容）。

    本测试由「已登记缺口」改写而来：原先固定「部分成交未建模」的旧行为，
    D5 实现后必须改为固定「默认不启用限制时行为不变」。
    """
    sim = _sim()
    bar = _bar("600000", DAY0, 10.0, open_=10.0, prev=10.0, volume=100.0)
    r = sim.try_fill("BUY", 1_000_000, bar)  # 未启用参与率上限 → 全额成交
    assert r.filled is True and r.quantity == 1_000_000
    assert r.partial is False
    assert r.participation_cap == 0


def test_capacity_limit_enabled_changes_the_same_scenario():
    """同一个场景启用参与率上限后必须部分成交 —— 否则 D5 等于没接线。"""
    sim = _sim(max_participation_rate=0.01, allow_partial_fill=True)
    bar = _bar("600000", DAY0, 10.0, open_=10.0, prev=10.0, volume=1_000_000.0)
    r = sim.try_fill("BUY", 1_000_000, bar)  # 上限 = 1% × 1,000,000 = 10,000 股
    assert r.participation_cap == 10_000
    assert r.filled is True and r.quantity == 10_000
    assert r.partial is True
    assert r.requested_quantity == 1_000_000


async def test_worker_applies_participation_config_from_task(db_session, monkeypatch):
    """D5：API 写进 config_json 的参与率上限必须真正进入组合回测执行。"""
    bars = _series("600000", 60, 10.0, step=0.05)

    async def fake_get_history(self, symbol, start, end, **kwargs):
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    monkeypatch.setattr(
        "app.strategies.registry.get_strategy", lambda name: Schedule(buy_at=2, sell_at=40)
    )

    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("1000000"),
        status=RUNNING,
        config_json=json.dumps(
            {"execution": {"max_participation_rate": 0.001, "allow_partial_fill": True}}
        ),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from sqlalchemy.orm import sessionmaker

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    )
    await worker._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED

    payload = json.loads(task.result)
    assert payload["partial_fill_count"] >= 1, "参与率上限未生效：没有任何部分成交"
    assert payload["min_capacity_multiple"] is not None
    assert payload["min_capacity_multiple"] < 1, "容量倍数应 <1（有成交超出当日可承受容量）"


async def test_worker_surfaces_bars_adjust_and_limit_reference_diagnostics(
    db_session, monkeypatch
):
    """D6：结果必须带实际复权口径，以及「昨收对齐不上」的根数（不能静默）。"""
    qfq_bars = _series("600000", 60, 10.0, step=0.05)
    # 未复权序列少最后一天 → 前复权那天的昨收无法还原
    none_bars = qfq_bars[:-1]

    async def fake_get_history(self, symbol, start, end, **kwargs):
        bars = none_bars if kwargs.get("adjust") == "none" else qfq_bars
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars), missing_dates=[]),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )
    monkeypatch.setattr(
        "app.strategies.registry.get_strategy", lambda name: Schedule(buy_at=2, sell_at=40)
    )

    task = PortfolioBacktest(
        symbols=json.dumps(["600000"]),
        strategy_name="ma_cross",
        start_time=datetime(2026, 1, 5),
        end_time=datetime(2026, 1, 5) + timedelta(days=59),
        initial_cash=Decimal("1000000"),
        status=RUNNING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from sqlalchemy.orm import sessionmaker

    from app.tasks import portfolio_worker as mod

    worker = mod.PortfolioBacktestWorker(
        session_factory=sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)
    )
    await worker._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED

    payload = json.loads(task.result)
    assert payload["bars_adjust"] == "qfq"
    assert payload["bars_adjust_label"] == "前复权"
    assert payload["limit_reference"] == "unadjusted_previous_close"
    assert payload["limit_reference_missing"] == 1
    assert payload["limit_reference_complete"] is False
    assert "涨跌停判断已跳过" in payload["limit_reference_note"]

