"""部分成交与容量测试（P0-05 的 D5）。

研发计划 §5.3.5 要求「报告…容量」，§7.6 要求「部分成交…单独验收」。
D5 缺口：原实现是**全成或全不成**，没有成交量参与率上限，于是大资金规模下的
「同一天买满」在回测里永远成立，容量问题被系统性隐藏。

本测试锁定：
* 未启用参与率限制时行为与改造前完全一致（默认 0，不引入回归）；
* 启用后超量委托按可成交量**部分成交**，且成交/拒绝两种模式都可选；
* 成交费用按**实际成交量**计算，而不是请求量；
* 容量倍数无法计算时返回 None（标注未知），不返回 0 冒充「完全不能做」。
"""
from __future__ import annotations

from app.backtest.execution import (
    ExecutionConfig,
    ExecutionSimulator,
    capacity_multiple,
    participation_cap,
)
from app.market_data.base import QuoteData


def _bar(volume: float = 1_000_000.0, price: float = 10.0, previous_close: float = 10.0) -> QuoteData:
    return QuoteData(
        symbol="600000",
        name="测试股",
        price=price,
        open=price,
        high=price * 1.02,
        low=price * 0.98,
        previous_close=previous_close,
        volume=volume,
        amount=volume * price,
    )


# ───────────── 默认行为不变 ─────────────


def test_default_config_has_no_participation_limit():
    config = ExecutionConfig()
    assert config.max_participation_rate == 0.0
    sim = ExecutionSimulator(config)
    result = sim.try_fill("BUY", 500_000, _bar(volume=1_000_000.0))
    assert result.filled is True
    assert result.quantity == 500_000  # 默认不限制，保持既有行为
    assert result.partial is False
    assert result.participation_cap == 0


# ───────────── 参与率上限 ─────────────


def test_participation_cap_rounds_down_to_lot():
    assert participation_cap(1_000_000.0, 0.10) == 100_000
    assert participation_cap(1_234.0, 0.10, lot_size=100) == 100
    assert participation_cap(0.0, 0.10) == 0
    assert participation_cap(1_000.0, 0.0) == 0
    assert participation_cap(1_000.0, -0.1) == 0


def test_partial_fill_when_exceeding_participation_rate():
    sim = ExecutionSimulator(ExecutionConfig(max_participation_rate=0.10))
    result = sim.try_fill("BUY", 200_000, _bar(volume=1_000_000.0))
    assert result.filled is True
    assert result.partial is True
    assert result.requested_quantity == 200_000
    assert result.quantity == 100_000           # 10% × 100 万股
    assert result.participation_cap == 100_000
    assert "部分成交" in result.reason


def test_fees_are_charged_on_filled_quantity_only():
    sim = ExecutionSimulator(
        ExecutionConfig(max_participation_rate=0.10, slippage=0.0, commission_rate=0.0003)
    )
    result = sim.try_fill("BUY", 200_000, _bar(volume=1_000_000.0, price=10.0))
    expected_value = 10.0 * 100_000
    assert result.commission == max(expected_value * 0.0003, 5.0)
    # 若按请求量 20 万股计费，佣金会是两倍
    assert result.commission < expected_value * 0.0003 * 1.5


def test_reject_instead_of_partial_when_disabled():
    sim = ExecutionSimulator(
        ExecutionConfig(max_participation_rate=0.10, allow_partial_fill=False)
    )
    result = sim.try_fill("BUY", 200_000, _bar(volume=1_000_000.0))
    assert result.filled is False
    assert "超过成交量容量上限" in result.reason
    assert result.requested_quantity == 200_000
    assert result.participation_cap == 100_000


def test_cap_below_one_lot_is_rejected():
    """可成交量不足一手时不能成交（不能凭空造出零股）。"""
    sim = ExecutionSimulator(ExecutionConfig(max_participation_rate=0.10))
    result = sim.try_fill("BUY", 100, _bar(volume=500.0))  # 10% → 50 股 → 0 手
    assert result.filled is False
    assert "超过成交量容量上限" in result.reason


def test_within_capacity_fills_fully():
    sim = ExecutionSimulator(ExecutionConfig(max_participation_rate=0.10))
    result = sim.try_fill("BUY", 50_000, _bar(volume=1_000_000.0))
    assert result.filled is True
    assert result.partial is False
    assert result.quantity == 50_000


def test_sell_side_also_respects_capacity():
    sim = ExecutionSimulator(ExecutionConfig(max_participation_rate=0.10))
    result = sim.try_fill("SELL", 300_000, _bar(volume=1_000_000.0))
    assert result.filled is True
    assert result.partial is True
    assert result.quantity == 100_000


def test_suspension_still_rejected_before_capacity_check():
    sim = ExecutionSimulator(ExecutionConfig(max_participation_rate=0.10))
    result = sim.try_fill("BUY", 100, _bar(volume=0.0))
    assert result.filled is False
    assert "停牌" in result.reason


# ───────────── 容量倍数 ─────────────


def test_capacity_multiple_basic():
    # 需求 5 万股、当日 100 万股、参与率 10% → 上限 10 万股 → 倍数 2.0
    assert capacity_multiple(50_000, 10.0, 1_000_000.0, 0.10) == 2.0
    # 需求 20 万股 → 上限 10 万股 → 倍数 0.5（超容量）
    assert capacity_multiple(200_000, 10.0, 1_000_000.0, 0.10) == 0.5


def test_capacity_multiple_returns_none_when_unknown():
    assert capacity_multiple(0, 10.0, 1_000_000.0, 0.10) is None
    assert capacity_multiple(100, 0.0, 1_000_000.0, 0.10) is None
    assert capacity_multiple(100, 10.0, 0.0, 0.10) is None
    assert capacity_multiple(100, 10.0, 1_000_000.0, 0.0) is None


# ───────────── 组合引擎级容量报告 ─────────────


def test_engine_reports_capacity_when_participation_enabled():
    """引擎必须把每笔成交的容量倍数汇总出来（计划要求「报告…容量」）。"""
    from app.portfolio.config import PortfolioConfig
    from app.portfolio.engine import PortfolioBacktestEngine

    from tests.test_portfolio import ScheduleStrategy, make_daily_bars

    hist = make_daily_bars("600000", n=30)
    # 压低成交量，使参与率上限真实生效：10% → 1 万股/日
    for bar in hist:
        bar.volume = 100_000.0

    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(
            initial_cash=1_000_000.0,
            execution=ExecutionConfig(max_participation_rate=0.10),
        ),
    )
    result = engine.run({"600000": hist})

    assert result.min_capacity_multiple is not None, "启用参与率后必须给出容量倍数"
    assert result.min_capacity_multiple <= 1.0
    assert result.partial_fill_count >= 1
    payload = result.to_dict()
    assert payload["min_capacity_multiple"] == result.min_capacity_multiple
    assert payload["partial_fill_count"] == result.partial_fill_count


def test_engine_capacity_is_none_when_participation_disabled():
    """未启用参与率限制时不得伪造容量数字，必须为 None（未知）。"""
    from app.portfolio.config import PortfolioConfig
    from app.portfolio.engine import PortfolioBacktestEngine

    from tests.test_portfolio import ScheduleStrategy, make_daily_bars

    hist = make_daily_bars("600000", n=30)
    engine = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(initial_cash=1_000_000.0),
    )
    result = engine.run({"600000": hist})
    assert result.min_capacity_multiple is None
    assert result.partial_fill_count == 0


# ───────────── 容量 → 账户规模换算（sustainable_aum） ─────────────


def test_sustainable_aum_is_min_over_trades_of_equity_times_multiple():
    """手算对照：逐笔算 equity × capacity_multiple，取最小那笔。"""
    from datetime import date

    from app.portfolio.engine import sustainable_aum

    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    equity = [100_000.0, 200_000.0]
    trades = [
        # d0：倍数 0.5 → 上限 50,000（最紧）
        {"time": "2026-01-05T00:00:00", "symbol": "600000", "side": "BUY",
         "quantity": 20_000, "capacity_multiple": 0.5},
        # d1：倍数 2.0 → 上限 400,000
        {"time": "2026-01-06T00:00:00", "symbol": "600000", "side": "BUY",
         "quantity": 5_000, "capacity_multiple": 2.0},
    ]
    out = sustainable_aum(trades, dates, equity)
    assert out is not None
    assert out["max_aum"] == 50_000.0
    assert out["binding_trade"]["date"] == "2026-01-05"
    assert out["binding_trade"]["capacity_multiple"] == 0.5
    assert out["binding_trade"]["account_equity_on_that_day"] == 100_000.0
    assert out["considered_trades"] == 2
    assert out["basis"] == "min(account_equity_on_trade_day × capacity_multiple)"
    # 必须是上界而不是承诺：注意事项要跟着结果一起返回
    assert any("无市场冲击" in item for item in out["assumptions"])


def test_sustainable_aum_none_when_capacity_not_modeled():
    """未启用参与率上限（倍数为 None）时必须返回 None，不能当成「容量无限」。"""
    from datetime import date

    from app.portfolio.engine import sustainable_aum

    trades = [{"time": "2026-01-05T00:00:00", "capacity_multiple": None}]
    assert sustainable_aum(trades, [date(2026, 1, 5)], [100_000.0]) is None
    assert sustainable_aum([], [date(2026, 1, 5)], [100_000.0]) is None


def test_sustainable_aum_skips_trades_it_cannot_price():
    """日期对不上、时间戳损坏、倍数为 0 的成交一律跳过，不猜、不崩。"""
    from datetime import date

    from app.portfolio.engine import sustainable_aum

    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    equity = [100_000.0, 100_000.0]
    trades = [
        {"time": "2026-01-09T00:00:00", "capacity_multiple": 0.1},  # 不在净值序列里
        {"time": "not-a-date", "capacity_multiple": 0.1},           # 时间戳损坏
        {"time": "2026-01-05T00:00:00", "capacity_multiple": 0.0},  # 倍数无效
        {"time": "2026-01-06T00:00:00", "capacity_multiple": 0.25}, # 唯一可用
    ]
    out = sustainable_aum(trades, dates, equity)
    assert out is not None
    assert out["considered_trades"] == 1
    assert out["max_aum"] == 25_000.0


def test_engine_reports_sustainable_aum_when_capacity_enabled():
    """引擎级：启用参与率上限时结果必须带 capacity 上界，未启用时为 None。"""
    from app.portfolio.config import PortfolioConfig
    from app.portfolio.engine import PortfolioBacktestEngine

    from tests.test_portfolio import ScheduleStrategy, make_daily_bars

    hist = make_daily_bars("600000", n=30)
    for bar in hist:
        bar.volume = 100_000.0

    enabled = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(
            initial_cash=1_000_000.0,
            execution=ExecutionConfig(max_participation_rate=0.10),
        ),
    ).run({"600000": hist})
    capacity = enabled.to_dict()["capacity"]
    assert capacity is not None
    assert capacity["max_aum"] > 0
    assert capacity["binding_trade"]["date"] in enabled.dates
    # 上界必须等于「成交日净值 × 该笔倍数」，且落在净值区间的合理带内。
    # （不能用初始资金当基数：盈利后成交日净值 > 初始资金，用初始资金会低估。）
    binding = capacity["binding_trade"]
    assert capacity["max_aum"] == round(
        binding["account_equity_on_that_day"] * binding["capacity_multiple"], 2
    )
    low = min(enabled.equity_curve) * enabled.min_capacity_multiple
    high = max(enabled.equity_curve) * enabled.min_capacity_multiple
    assert low - 1e-6 <= capacity["max_aum"] <= high + 1e-6

    disabled = PortfolioBacktestEngine(
        strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=12)},
        config=PortfolioConfig(initial_cash=1_000_000.0),
    ).run({"600000": hist})
    assert disabled.to_dict()["capacity"] is None
