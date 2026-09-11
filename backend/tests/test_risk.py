"""风控管理器测试。"""
from app.config import Settings
from app.risk.limits import RiskLimits, limits_from_settings
from app.risk.risk_manager import AccountSnapshot, RiskManager

from tests.helpers import make_quote


def _snapshot(**kwargs) -> AccountSnapshot:
    defaults = dict(
        total_asset=100_000.0,
        available_cash=100_000.0,
        initial_cash=100_000.0,
        current_position_value={},
        daily_pnl=0.0,
        peak_asset=100_000.0,
    )
    defaults.update(kwargs)
    return AccountSnapshot(**defaults)


def test_buy_ok():
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    decision = rm.check_buy(_snapshot(), "600000", 10_000.0, quote)
    assert decision.allowed is True


def test_limits_from_settings_uses_risk_env_fields():
    settings = Settings(
        database_url="sqlite:///:memory:",
        risk_max_position_per_symbol=0.5,
        risk_commission_rate=0.001,
    )

    limits = limits_from_settings(settings)

    assert limits.max_position_per_symbol == 0.5
    assert limits.commission_rate == 0.001
    # 未覆盖的字段回退到默认值，避免配置缺失时风控消失
    assert limits.max_total_position == RiskLimits().max_total_position


def test_limits_from_settings_falls_back_for_missing_attrs():
    limits = limits_from_settings(object())

    assert limits == RiskLimits()


def test_risk_manager_honours_overridden_limits():
    rm = RiskManager(RiskLimits(max_position_per_symbol=0.01))
    quote = make_quote(symbol="600000", price=10.0)

    decision = rm.check_buy(_snapshot(), "600000", 10_000.0, quote)

    assert decision.allowed is False
    assert "仓位" in decision.reason


def test_buy_stale_quote_rejected():
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0, is_stale=True)
    decision = rm.check_buy(_snapshot(), "600000", 10_000.0, quote)
    assert decision.allowed is False
    assert "过期" in decision.reason


def test_buy_insufficient_cash():
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    snapshot = _snapshot(available_cash=1_000.0)
    decision = rm.check_buy(snapshot, "600000", 10_000.0, quote)
    assert decision.allowed is False
    assert "资金不足" in decision.reason


def test_buy_position_limit():
    """单只股票仓位不得超过 20%。"""
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    # 已有 15% 仓位，再买 10% 将超过 20%
    snapshot = _snapshot(current_position_value={"600000": 15_000.0})
    decision = rm.check_buy(snapshot, "600000", 10_000.0, quote)
    assert decision.allowed is False
    assert "仓位" in decision.reason


def test_buy_total_position_limit():
    """总仓位不得超过 80%。"""
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    snapshot = _snapshot(current_position_value={"000001": 75_000.0})
    decision = rm.check_buy(snapshot, "600000", 10_000.0, quote)
    assert decision.allowed is False
    assert "总仓位" in decision.reason


def test_buy_daily_loss_limit():
    """单日亏损 3% 后禁止买入。"""
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    snapshot = _snapshot(daily_pnl=-3_000.0, total_asset=100_000.0)
    decision = rm.check_buy(snapshot, "600000", 5_000.0, quote)
    assert decision.allowed is False
    assert "单日亏损" in decision.reason


def test_buy_drawdown_limit():
    """总回撤 10% 后禁止新增仓位。"""
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    # 峰值 100 万，当前 85 万，回撤 15%
    snapshot = _snapshot(total_asset=85_000.0, peak_asset=100_000.0, available_cash=50_000.0)
    decision = rm.check_buy(snapshot, "600000", 5_000.0, quote)
    assert decision.allowed is False
    assert "回撤" in decision.reason


def test_sell_insufficient_position():
    """卖出超过可用持仓被拒绝。"""
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0)
    decision = rm.check_sell(_snapshot(), "600000", 100, 50, quote)
    assert decision.allowed is False
    assert "可用持仓" in decision.reason


def test_sell_stale_rejected():
    rm = RiskManager()
    quote = make_quote(symbol="600000", price=10.0, is_stale=True)
    decision = rm.check_sell(_snapshot(), "600000", 100, 100, quote)
    assert decision.allowed is False
