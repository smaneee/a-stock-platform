"""QMT live adapter safety and contract tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.live_trading import QmtBrokerError, QmtLiveBroker


class _FakeTrader:
    stopped = False
    last_order = None

    def __init__(self, path, session_id):
        self.path = path
        self.session_id = session_id

    def start(self):
        return None

    def connect(self):
        return 0

    def subscribe(self, account):
        return 0

    def query_stock_asset(self, account):
        return SimpleNamespace(cash=80_000, total_asset=100_000)

    def query_stock_positions(self, account):
        return [
            SimpleNamespace(
                stock_code="600001.SH", volume=2_000, can_use_volume=1_500
            )
        ]

    def query_stock_orders(self, account):
        return [
            SimpleNamespace(
                stock_code="600001.SH",
                order_id=12345,
                order_type=23,
                order_volume=100,
                price=10.25,
                traded_volume=100,
                traded_price=10.24,
                order_status=56,
                status_msg="已成",
                order_remark="live-plan-7",
            )
        ]

    def order_stock(self, *args):
        type(self).last_order = args
        return 12345

    def stop(self):
        type(self).stopped = True


class _FakeAccount:
    def __init__(self, account_id, account_type):
        self.account_id = account_id
        self.account_type = account_type


_CONSTANTS = SimpleNamespace(
    STOCK_BUY=23,
    STOCK_SELL=24,
    FIX_PRICE=11,
    ORDER_UNREPORTED=48,
    ORDER_WAIT_REPORTING=49,
    ORDER_REPORTED=50,
    ORDER_REPORTED_CANCEL=51,
    ORDER_PARTSUCC_CANCEL=52,
    ORDER_PART_CANCEL=53,
    ORDER_CANCELED=54,
    ORDER_PART_SUCC=55,
    ORDER_SUCCEEDED=56,
    ORDER_JUNK=57,
    ORDER_UNKNOWN=255,
)


def _settings(tmp_path, **overrides):
    values = {
        "real_trading_enabled": True,
        "qmt_userdata_path": str(tmp_path),
        "qmt_account_id": "test-account",
        "qmt_account_type": "STOCK",
        "qmt_call_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _loader():
    return _FakeTrader, _FakeAccount, _CONSTANTS, None


@pytest.mark.asyncio
async def test_connect_query_and_place_limit_order(tmp_path):
    broker = QmtLiveBroker(_settings(tmp_path), sdk_loader=_loader)

    await broker.connect()
    snapshot = await broker.query_account()
    order_id = await broker.place_limit_order(
        "600001", "BUY", 100, 10.25, "confirmed-plan-1"
    )
    await broker.close()

    assert snapshot.cash == 80_000
    assert snapshot.positions["600001"].available_quantity == 1_500
    assert order_id == 12345
    assert _FakeTrader.last_order[1] == "600001.SH"
    assert _FakeTrader.last_order[2] == 23
    assert _FakeTrader.stopped is True


@pytest.mark.asyncio
async def test_query_orders_maps_qmt_status_and_remark(tmp_path):
    broker = QmtLiveBroker(_settings(tmp_path), sdk_loader=_loader)

    await broker.connect()
    orders = await broker.query_orders(remark_prefix="live-plan-7")
    await broker.close()

    assert len(orders) == 1
    assert orders[0].order_id == 12345
    assert orders[0].symbol == "600001"
    assert orders[0].side == "BUY"
    assert orders[0].status == "FILLED"
    assert orders[0].traded_volume == 100


@pytest.mark.asyncio
async def test_real_trading_disabled_is_hard_block(tmp_path):
    broker = QmtLiveBroker(
        _settings(tmp_path, real_trading_enabled=False), sdk_loader=_loader
    )

    with pytest.raises(QmtBrokerError, match="实盘通道已锁定"):
        await broker.connect()


@pytest.mark.asyncio
async def test_missing_account_or_path_is_explicit(tmp_path):
    broker = QmtLiveBroker(
        _settings(tmp_path, qmt_account_id=""), sdk_loader=_loader
    )
    with pytest.raises(QmtBrokerError, match="QMT_ACCOUNT_ID"):
        await broker.connect()

    missing = QmtLiveBroker(
        _settings(tmp_path, qmt_userdata_path=str(tmp_path / "missing")),
        sdk_loader=_loader,
    )
    with pytest.raises(QmtBrokerError, match="QMT_USERDATA_PATH"):
        await missing.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "side", "quantity", "price", "message"),
    [
        ("bad", "BUY", 100, 10, "非法 A 股代码"),
        ("600001", "HOLD", 100, 10, "BUY/SELL"),
        ("600001", "BUY", 150, 10, "100 股整数倍"),
        ("600001", "BUY", 100, 0, "限价必须大于 0"),
    ],
)
async def test_order_input_validation(
    tmp_path, symbol, side, quantity, price, message
):
    broker = QmtLiveBroker(_settings(tmp_path), sdk_loader=_loader)
    await broker.connect()

    with pytest.raises(QmtBrokerError, match=message):
        await broker.place_limit_order(symbol, side, quantity, price, "test")


def test_beijing_exchange_symbol_mapping():
    assert QmtLiveBroker._qmt_symbol("920001") == "920001.BJ"
    assert QmtLiveBroker._qmt_symbol("830001") == "830001.BJ"


def test_live_status_route_is_read_only():
    from app.main import app

    operation = app.openapi()["paths"]["/api/live/status"]
    assert set(operation) == {"get"}
