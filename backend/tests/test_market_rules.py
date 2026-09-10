"""统一交易规则引擎测试。"""
from decimal import Decimal

import pytest

from app.market_rules.rules import (
    Board,
    MarketRuleEngine,
    NO_LIMIT,
)


@pytest.fixture
def engine():
    return MarketRuleEngine()


# ──────── 板块分类 ────────


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600000", Board.SH_MAIN),
        ("601318", Board.SH_MAIN),
        ("603288", Board.SH_MAIN),
        ("000001", Board.SZ_MAIN),
        ("002594", Board.SZ_MAIN),
        ("300750", Board.CHINEXT),
        ("301001", Board.CHINEXT),
        ("688981", Board.STAR),
        ("830799", Board.BSE),
        ("430047", Board.BSE),
        ("920001", Board.BSE),
        ("sh600000", Board.SH_MAIN),
        ("sz000001", Board.SZ_MAIN),
        ("bj830799", Board.BSE),
    ],
)
def test_classify(engine, symbol, expected):
    assert engine.classify(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    ["abc", "12345", "1234567", "", None, "60abcd"],
)
def test_classify_unknown(engine, symbol):
    assert engine.classify(symbol) == Board.UNKNOWN


# ──────── 涨跌停幅度 ────────


def test_price_limit_main_board(engine):
    rules = engine.get_rules("600000", name="浦发银行")
    assert rules.price_limit == Decimal("0.10")
    assert rules.has_price_limit is True


def test_price_limit_st(engine):
    rules = engine.get_rules("600000", name="ST测试")
    assert rules.price_limit == Decimal("0.05")
    assert rules.is_st is True

    # 显式 is_st=True 优先于名称
    rules2 = engine.get_rules("600000", name="正常", is_st=True)
    assert rules2.price_limit == Decimal("0.05")


def test_price_limit_star_st(engine):
    rules = engine.get_rules("600000", name="*ST测试")
    assert rules.price_limit == Decimal("0.05")


def test_price_limit_chinext_star(engine):
    assert engine.get_rules("300750", name="宁德时代").price_limit == Decimal("0.20")
    assert engine.get_rules("688981", name="中芯国际").price_limit == Decimal("0.20")


def test_price_limit_bse(engine):
    assert engine.get_rules("830799", name="测试").price_limit == Decimal("0.30")


def test_new_listing_no_limit(engine):
    rules = engine.get_rules("600000", name="新股", is_new_listing=True)
    assert rules.price_limit == NO_LIMIT
    assert rules.has_price_limit is False
    assert rules.is_new_listing is True


# ──────── 涨跌停价计算 ────────


def test_limit_up_down_main_board(engine):
    rules = engine.get_rules("600000", name="浦发银行")
    prev = Decimal("10.00")
    assert rules.limit_up(prev) == Decimal("11.00")
    assert rules.limit_down(prev) == Decimal("9.00")


def test_limit_up_down_st(engine):
    rules = engine.get_rules("600000", name="ST测试")
    prev = Decimal("10.00")
    assert rules.limit_up(prev) == Decimal("10.50")
    assert rules.limit_down(prev) == Decimal("9.50")


def test_limit_up_down_rounding(engine):
    """涨停价需按最小报价单位四舍五入到分。"""
    rules = engine.get_rules("600000", name="浦发银行")
    prev = Decimal("10.05")
    # 10.05 * 1.10 = 11.055 -> 四舍五入 11.06
    assert rules.limit_up(prev) == Decimal("11.06")
    # 10.05 * 0.90 = 9.045 -> 四舍五入 9.05
    assert rules.limit_down(prev) == Decimal("9.05")


def test_limit_none_for_no_limit_and_bad_prev(engine):
    rules = engine.get_rules("600000", name="新股", is_new_listing=True)
    assert rules.limit_up(Decimal("10.00")) is None
    assert rules.limit_down(Decimal("10.00")) is None

    normal = engine.get_rules("600000", name="浦发银行")
    assert normal.limit_up(Decimal("0")) is None
    assert normal.limit_down(Decimal("-1")) is None


# ──────── 数量与价格 ────────


def test_validate_buy_quantity(engine):
    assert engine.validate_buy_quantity(100) == (True, "")
    assert engine.validate_buy_quantity(200) == (True, "")
    assert engine.validate_buy_quantity(150)[0] is False
    assert engine.validate_buy_quantity(0)[0] is False
    assert engine.validate_buy_quantity(-100)[0] is False


def test_validate_sell_quantity_odd_lot(engine):
    # 卖出允许不足一手零股
    assert engine.validate_sell_quantity(50, 100) == (True, "")
    assert engine.validate_sell_quantity(100, 100) == (True, "")
    # 但不能超卖
    assert engine.validate_sell_quantity(150, 100)[0] is False
    assert engine.validate_sell_quantity(0, 100)[0] is False


def test_round_price(engine):
    assert engine.round_price(Decimal("10.055")) == Decimal("10.06")
    assert engine.round_price(10.045) == Decimal("10.05")
    assert engine.round_price("10.00") == Decimal("10.00")


# ──────── ST 名称判断 ────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ST国重装", True),
        ("*ST美尚", True),
        ("SST中华", True),
        ("浦发银行", False),
        ("", False),
        ("测试", False),
    ],
)
def test_is_st_name(name, expected):
    assert MarketRuleEngine.is_st_name(name) is expected
