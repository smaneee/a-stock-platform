"""M1 修正提交测试：统一交易规则引擎、SecurityMaster、批次 T+1。

覆盖：
- SecurityMasterService 的 ensure/get/is_new_listing/list_count。
- MarketRuleEngine.get_rules_from_security 根据 SecurityMaster 主数据自动判断
  新股上市初期无涨跌停阶段（含主板/创业板/科创板/北交所四档）。
- PaperBroker 行情价格非最小报价单位（TICK_SIZE）拒绝。
- PaperBroker 同一账户多批次买入与 FIFO 卖出平账。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.database.models import PaperAccount, PaperPosition, Security
from app.market_rules.rules import (
    Board,
    LOT_SIZE,
    MarketRuleEngine,
    NO_LIMIT,
    TICK_SIZE,
)
from app.market_rules.security_master import SecurityMasterService
from app.paper_trading.broker import PaperBroker

from tests.helpers import make_quote


# ──────── SecurityMaster 主数据服务 ────────


class TestSecurityMasterService:
    """SecurityMaster 主数据读写与新股判定。"""

    def test_ensure_creates_when_missing(self, db_session):
        svc = SecurityMasterService(db_session)
        sec = svc.ensure("600000", name="浦发银行")
        assert sec.symbol == "600000"
        assert sec.name == "浦发银行"
        assert sec.board == "sh_main"
        assert sec.is_st is False
        assert sec.listing_date is None
        assert sec.source == "manual"

    def test_ensure_idempotent_does_not_overwrite_existing(self, db_session):
        """完全相同的输入不修改记录（幂等）。"""
        svc = SecurityMasterService(db_session)
        first = svc.ensure("600000", name="浦发银行", listing_date=date(2000, 1, 1))
        updated_at_first = first.updated_at
        again = svc.ensure("600000", name="浦发银行", listing_date=date(2000, 1, 1))
        assert again.symbol == first.symbol
        assert again.name == "浦发银行"
        assert again.listing_date == date(2000, 1, 1)
        # 没有任何字段改变 → updated_at 不变
        assert again.updated_at == updated_at_first

    def test_ensure_updates_when_field_differs(self, db_session):
        """字段不一致时更新（允许纠正错误数据）。"""
        svc = SecurityMasterService(db_session)
        first = svc.ensure("600000", name="浦发银行", is_st=False)
        again = svc.ensure("600000", is_st=True)
        # is_st 显式传值可纠正
        assert again.is_st is True
        # name 是显式传值但已存在非空，未覆盖
        assert again.name == "浦发银行"

    def test_ensure_backfills_missing_fields_only(self, db_session):
        """已存在但某字段为空时，新提供的字段可补齐。"""
        svc = SecurityMasterService(db_session)
        svc.ensure("600000", name="")  # 空名称
        sec = svc.ensure("600000", name="浦发银行")
        assert sec.name == "浦发银行"

    def test_ensure_classifies_chinext_star_bse(self, db_session):
        svc = SecurityMasterService(db_session)
        assert svc.ensure("300750").board == "chinext"
        assert svc.ensure("688981").board == "star"
        assert svc.ensure("830799").board == "bse"
        assert svc.ensure("000001").board == "sz_main"
        assert svc.ensure("601318").board == "sh_main"

    def test_get_returns_none_for_missing(self, db_session):
        svc = SecurityMasterService(db_session)
        assert svc.get("999999") is None

    def test_list_count(self, db_session):
        svc = SecurityMasterService(db_session)
        svc.ensure("600000", name="A")
        svc.ensure("000001", name="B")
        assert svc.list_count() == 2

    def test_is_new_listing_main_first_day(self, db_session):
        svc = SecurityMasterService(db_session)
        listing = date(2024, 6, 10)
        svc.ensure("601000", name="新股", listing_date=listing)
        # 主板 1 天无涨跌停
        assert svc.is_new_listing("601000", listing) is True
        assert svc.is_new_listing("601000", listing + timedelta(days=1)) is False

    def test_is_new_listing_chinext_first_five_days(self, db_session):
        svc = SecurityMasterService(db_session)
        listing = date(2024, 6, 10)
        svc.ensure("301000", name="新股", listing_date=listing)
        # 创业板 5 个交易日内均无涨跌停
        assert svc.is_new_listing("301000", listing) is True
        assert svc.is_new_listing("301000", listing + timedelta(days=4)) is True
        assert svc.is_new_listing("301000", listing + timedelta(days=5)) is False

    def test_is_new_listing_without_listing_date_is_false(self, db_session):
        svc = SecurityMasterService(db_session)
        svc.ensure("600000", name="浦发")  # 无 listing_date
        assert svc.is_new_listing("600000", date(2024, 1, 1)) is False

    def test_is_new_listing_with_none_date_returns_false(self, db_session):
        svc = SecurityMasterService(db_session)
        svc.ensure("600000", name="浦发", listing_date=date(2024, 1, 1))
        assert svc.is_new_listing("600000", None) is False


# ──────── get_rules_from_security：基于主数据推导规则 ────────


class TestGetRulesFromSecurity:
    """MarketRuleEngine.get_rules_from_security 必须根据 SecurityMaster 推导
    board/is_st/is_new_listing，而不是依赖调用方临时传参。"""

    def _make(self, db_session, *, symbol, name="", is_st=False, listing=None, board=None):
        sec = Security(
            symbol=symbol,
            name=name,
            board=board or MarketRuleEngine.classify(symbol).value,
            is_st=is_st,
            listing_date=listing,
            source="manual",
        )
        db_session.add(sec)
        db_session.flush()
        return sec

    def test_new_listing_main_uses_no_limit(self, db_session):
        listing = date(2024, 6, 10)
        sec = self._make(db_session, symbol="601000", name="新股", listing=listing)
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec, listing)
        # 主板首日 → 无涨跌停
        assert rules.is_new_listing is True
        assert rules.price_limit == NO_LIMIT
        assert rules.has_price_limit is False
        assert rules.is_st is False

    def test_normal_main_uses_ten_percent(self, db_session):
        listing = date(2020, 1, 1)
        sec = self._make(db_session, symbol="600000", name="浦发银行", listing=listing)
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec, date(2024, 6, 10))
        assert rules.board == Board.SH_MAIN
        assert rules.price_limit == Decimal("0.10")
        assert rules.is_st is False

    def test_chinext_first_five_days_no_limit(self, db_session):
        listing = date(2024, 6, 10)
        sec = self._make(db_session, symbol="301000", name="宁德时代", listing=listing)
        engine = MarketRuleEngine()
        for offset in range(0, 5):
            rules = engine.get_rules_from_security(sec, listing + timedelta(days=offset))
            assert rules.is_new_listing is True, f"day {offset}"
            assert rules.price_limit == NO_LIMIT, f"day {offset}"
        # 第 6 天恢复 20%
        rules = engine.get_rules_from_security(sec, listing + timedelta(days=5))
        assert rules.is_new_listing is False
        assert rules.price_limit == Decimal("0.20")

    def test_star_first_five_days_no_limit(self, db_session):
        listing = date(2024, 6, 10)
        sec = self._make(db_session, symbol="688000", name="中芯国际", listing=listing)
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec, listing)
        assert rules.is_new_listing is True
        assert rules.price_limit == NO_LIMIT

    def test_bse_first_five_days_no_limit(self, db_session):
        listing = date(2024, 6, 10)
        sec = self._make(db_session, symbol="830799", name="北交所新股", listing=listing)
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec, listing + timedelta(days=2))
        assert rules.is_new_listing is True
        assert rules.price_limit == NO_LIMIT

    def test_st_name_overrides_listing_no_limit(self, db_session):
        """ST 状态优先于新股无涨跌停，强制按 5% 计算（即使在上市初期）。"""
        listing = date(2024, 6, 10)
        # 通过 ensure 让 Security 自动从名称检测到 ST
        svc = SecurityMasterService(db_session)
        sec = svc.ensure("600000", name="*ST测试", listing_date=listing)
        # ensure 已将 is_st 自动设为 True（基于名称）
        assert sec.is_st is True
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec, listing)
        assert rules.is_st is True
        assert rules.price_limit == Decimal("0.05")

    def test_security_none_falls_back(self, db_session):
        """Security 为 None 时通过 fallback_name 推导（兼容回测无 DB 场景）。"""
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(None, fallback_name="浦发银行")
        assert rules.is_st is False

    def test_lot_and_tick(self, db_session):
        sec = self._make(db_session, symbol="600000", name="浦发")
        engine = MarketRuleEngine()
        rules = engine.get_rules_from_security(sec)
        assert rules.lot_size == LOT_SIZE
        assert rules.tick_size == TICK_SIZE


# ──────── PaperBroker：最小报价单位与多批次平账 ────────


def _create_account(db, cash=200_000.0) -> PaperAccount:
    account = PaperAccount(
        name="测试账户",
        initial_cash=Decimal(str(cash)),
        available_cash=Decimal(str(cash)),
        frozen_cash=Decimal("0"),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


class TestPaperBrokerRulesAndBatches:
    """PaperBroker 必须使用统一 MarketRuleEngine + SecurityMaster。"""

    def test_invalid_tick_size_quote_rejected(self, db_session):
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        # 价格 10.005 不在 0.01 最小报价单位上
        quote = make_quote(symbol="600000", price=10.005)
        order, error = broker.place_order(
            account.id, "600000", "BUY", 100, 10.005, quote=quote
        )
        assert order is None
        assert "最小报价单位" in error

    def test_valid_tick_size_quote_accepted(self, db_session):
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        quote = make_quote(symbol="600000", price=10.05)
        order, error = broker.place_order(
            account.id, "600000", "BUY", 100, 10.05, quote=quote
        )
        assert order is not None, error

    def test_two_buys_create_two_batches(self, db_session):
        """同一账户同日两次买入创建两个独立批次（acquisition_date 相同视为不同行）。"""
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        today = date(2024, 6, 10)
        q = make_quote(symbol="600000", price=10.0)

        order1, _ = broker.place_order(
            account.id, "600000", "BUY", 100, 10.0,
            quote=q, trading_date=today,
        )
        order2, _ = broker.place_order(
            account.id, "600000", "BUY", 200, 10.0,
            quote=q, trading_date=today,
        )
        assert order1 is not None and order2 is not None

        positions = db_session.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()
        assert len(positions) == 2
        assert all(p.acquisition_date == today for p in positions)
        assert all(p.available_quantity == 0 for p in positions)  # 当日冻结
        assert sum(p.quantity for p in positions) == 300

    def test_settle_then_sell_consumes_oldest_batch_first(self, db_session):
        """settle_t1 后按 acquisition_date FIFO 卖出。"""
        account = _create_account(db_session)
        broker = PaperBroker(db_session)

        day1 = date(2024, 6, 10)
        day2 = date(2024, 6, 11)
        q1 = make_quote(symbol="600000", price=10.0)
        q2 = make_quote(symbol="600000", price=10.0)

        order1, _ = broker.place_order(
            account.id, "600000", "BUY", 100, 10.0, quote=q1, trading_date=day1,
        )
        order2, _ = broker.place_order(
            account.id, "600000", "BUY", 200, 10.0, quote=q2, trading_date=day2,
        )
        # 当日合计 300 股，两批次均冻结
        positions = db_session.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()
        assert len(positions) == 2

        # 显式调用 settle_t1 解冻所有批次
        broker.settle_t1(account.id, trading_date=day2)
        assert all(p.available_quantity == p.quantity for p in positions)

        # 在 day2 卖出 150 股：FIFO 从最早批次扣减
        sell_quote = make_quote(symbol="600000", price=11.0)
        order, err = broker.place_order(
            account.id, "600000", "SELL", 150, 11.0,
            quote=sell_quote, trading_date=day2,
        )
        assert order is not None, err

        positions = db_session.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()
        # 最早批次 100 股应被卖光并删除，第二批次减至 150 股
        assert len(positions) == 1
        assert positions[0].quantity == 150
        assert positions[0].acquisition_date == day2

    def test_sell_today_without_settle_fails(self, db_session):
        """当日买入未 settle 直接卖出应失败（无可用持仓）。"""
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        today = date(2024, 6, 10)
        quote = make_quote(symbol="600000", price=10.0)
        broker.place_order(
            account.id, "600000", "BUY", 100, 10.0, quote=quote, trading_date=today
        )
        # 当日直接卖出，无 settle
        order, err = broker.place_order(
            account.id, "600000", "SELL", 100, 10.0, quote=quote, trading_date=today
        )
        assert order is None
        assert ("可用" in err) or ("持仓" in err) or ("T+1" in err)

    def test_sell_on_next_day_works_with_acquisition_date_lt(self, db_session):
        """次日卖出：T+1 通过 acquisition_date < trading_date 路径。"""
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        day1 = date(2024, 6, 10)
        day2 = date(2024, 6, 11)
        quote = make_quote(symbol="600000", price=10.0)

        broker.place_order(
            account.id, "600000", "BUY", 100, 10.0,
            quote=quote, trading_date=day1,
        )
        # 次日卖出（不 settle，走 acquisition_date < trading_date 路径）
        sell_quote = make_quote(symbol="600000", price=11.0)
        order, err = broker.place_order(
            account.id, "600000", "SELL", 100, 11.0,
            quote=sell_quote, trading_date=day2,
        )
        assert order is not None, err
        # 卖光后批次删除
        positions = db_session.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()
        assert positions == []

    def test_security_master_used_in_broker(self, db_session):
        """PaperBroker 下单时必须调用 SecurityMaster.ensure，确保 Security 行被建立。"""
        account = _create_account(db_session)
        broker = PaperBroker(db_session)
        quote = make_quote(symbol="600000", price=10.0)
        broker.place_order(
            account.id, "600000", "BUY", 100, 10.0, quote=quote
        )
        sec = db_session.get(Security, "600000")
        assert sec is not None
        assert sec.board == "sh_main"
        assert sec.source == "manual"
