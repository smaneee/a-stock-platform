"""Thin, timeout-bounded adapter around the locally installed xtquant SDK."""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import Settings, get_settings


class QmtBrokerError(RuntimeError):
    """QMT is unavailable or rejected an operation."""


class QmtBrokerTimeout(QmtBrokerError):
    """The caller cannot know whether a timed-out operation reached QMT."""


@dataclass(frozen=True)
class LivePosition:
    symbol: str
    quantity: int
    available_quantity: int


@dataclass(frozen=True)
class LiveAccountSnapshot:
    cash: float
    total_asset: float
    positions: dict[str, LivePosition]


@dataclass(frozen=True)
class LiveOrderStatus:
    order_id: int
    symbol: str
    side: str
    quantity: int
    price: float
    traded_volume: int
    traded_price: float
    raw_status: int
    status: str
    status_message: str
    remark: str


class QmtLiveBroker:
    """Synchronous xtquant client exposed through bounded async methods."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sdk_loader: Callable[[], tuple[Any, Any, Any, Any]] | None = None,
    ):
        self._settings = settings or get_settings()
        self._sdk_loader = sdk_loader or self._load_sdk
        self._trader: Any = None
        self._account: Any = None
        self._constants: Any = None

    async def connect(self) -> None:
        self._validate_config()
        await self._bounded(self._connect_sync)

    async def query_account(self) -> LiveAccountSnapshot:
        self._require_connected()
        return await self._bounded(self._query_account_sync)

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        remark: str,
    ) -> int:
        self._require_connected()
        if side not in {"BUY", "SELL"}:
            raise QmtBrokerError("side 仅支持 BUY/SELL")
        if quantity <= 0 or quantity % 100 != 0:
            raise QmtBrokerError("实盘数量必须为正的 100 股整数倍")
        if price <= 0:
            raise QmtBrokerError("实盘限价必须大于 0")
        return await self._bounded(
            lambda: self._place_limit_order_sync(
                symbol, side, quantity, price, remark
            )
        )

    async def query_orders(self, remark_prefix: str | None = None) -> list[LiveOrderStatus]:
        self._require_connected()
        orders = await self._bounded(self._query_orders_sync)
        if remark_prefix:
            orders = [
                order for order in orders if order.remark.startswith(remark_prefix)
            ]
        return orders

    async def close(self) -> None:
        if self._trader is not None:
            try:
                await self._bounded(self._trader.stop)
            finally:
                self._trader = None
                self._account = None

    def _validate_config(self) -> None:
        if not self._settings.real_trading_enabled:
            raise QmtBrokerError("REAL_TRADING_ENABLED=false，实盘通道已锁定")
        if not self._settings.qmt_userdata_path or not Path(
            self._settings.qmt_userdata_path
        ).is_dir():
            raise QmtBrokerError("QMT_USERDATA_PATH 不存在或不是目录")
        if not self._settings.qmt_account_id:
            raise QmtBrokerError("QMT_ACCOUNT_ID 未配置")

    def _connect_sync(self) -> None:
        XtQuantTrader, StockAccount, constants, _ = self._sdk_loader()
        trader = XtQuantTrader(
            self._settings.qmt_userdata_path,
            secrets.randbelow(2_000_000_000) + 1,
        )
        trader.start()
        if trader.connect() != 0:
            trader.stop()
            raise QmtBrokerError("无法连接 MiniQMT 客户端")
        account = StockAccount(
            self._settings.qmt_account_id,
            self._settings.qmt_account_type,
        )
        if trader.subscribe(account) != 0:
            trader.stop()
            raise QmtBrokerError("无法订阅 QMT 资金账号")
        self._trader = trader
        self._account = account
        self._constants = constants

    def _query_account_sync(self) -> LiveAccountSnapshot:
        asset = self._trader.query_stock_asset(self._account)
        if asset is None:
            raise QmtBrokerError("QMT 未返回账户资产")
        raw_positions = self._trader.query_stock_positions(self._account) or []
        positions: dict[str, LivePosition] = {}
        for item in raw_positions:
            symbol = str(item.stock_code).split(".", 1)[0]
            positions[symbol] = LivePosition(
                symbol=symbol,
                quantity=int(item.volume),
                available_quantity=int(item.can_use_volume),
            )
        return LiveAccountSnapshot(
            cash=float(asset.cash),
            total_asset=float(asset.total_asset),
            positions=positions,
        )

    def _place_limit_order_sync(
        self, symbol: str, side: str, quantity: int, price: float, remark: str
    ) -> int:
        order_type = (
            self._constants.STOCK_BUY if side == "BUY" else self._constants.STOCK_SELL
        )
        order_id = self._trader.order_stock(
            self._account,
            self._qmt_symbol(symbol),
            order_type,
            quantity,
            self._constants.FIX_PRICE,
            price,
            "a-stock-platform",
            remark[:24],
        )
        if not isinstance(order_id, int) or order_id <= 0:
            raise QmtBrokerError(f"QMT 拒绝委托，返回值={order_id!r}")
        return order_id

    def _query_orders_sync(self) -> list[LiveOrderStatus]:
        raw_orders = self._trader.query_stock_orders(self._account) or []
        return [self._map_order_status(item) for item in raw_orders]

    def _map_order_status(self, item: Any) -> LiveOrderStatus:
        raw_symbol = str(getattr(item, "stock_code", ""))
        symbol = raw_symbol.split(".", 1)[0]
        order_type = int(getattr(item, "order_type", 0) or 0)
        side = "BUY" if order_type == int(self._constants.STOCK_BUY) else "SELL"
        raw_status = int(getattr(item, "order_status", 0) or 0)
        return LiveOrderStatus(
            order_id=int(getattr(item, "order_id", 0) or 0),
            symbol=symbol,
            side=side,
            quantity=int(getattr(item, "order_volume", 0) or 0),
            price=float(getattr(item, "price", 0.0) or 0.0),
            traded_volume=int(getattr(item, "traded_volume", 0) or 0),
            traded_price=float(getattr(item, "traded_price", 0.0) or 0.0),
            raw_status=raw_status,
            status=self._normalize_order_status(raw_status),
            status_message=str(getattr(item, "status_msg", "") or ""),
            remark=str(getattr(item, "order_remark", "") or ""),
        )

    async def _bounded(self, call: Callable[[], Any]) -> Any:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(call),
                timeout=self._settings.qmt_call_timeout_seconds,
            )
        except TimeoutError as exc:
            raise QmtBrokerTimeout("QMT 调用超时，委托结果未知，禁止自动重试") from exc
        except QmtBrokerError:
            raise
        except Exception as exc:
            raise QmtBrokerError(f"QMT 调用失败: {exc}") from exc

    def _require_connected(self) -> None:
        if self._trader is None or self._account is None:
            raise QmtBrokerError("QMT 尚未连接")

    @staticmethod
    def _qmt_symbol(symbol: str) -> str:
        if len(symbol) != 6 or not symbol.isdigit():
            raise QmtBrokerError("非法 A 股代码")
        if symbol.startswith(("4", "8", "92")):
            exchange = "BJ"
        else:
            exchange = "SH" if symbol.startswith(("5", "6", "9")) else "SZ"
        return f"{symbol}.{exchange}"

    def _normalize_order_status(self, raw_status: int) -> str:
        constants = self._constants
        if raw_status == int(getattr(constants, "ORDER_SUCCEEDED", 56)):
            return "FILLED"
        if raw_status == int(getattr(constants, "ORDER_PART_SUCC", 55)):
            return "PARTIAL_FILLED"
        if raw_status in {
            int(getattr(constants, "ORDER_CANCELED", 54)),
            int(getattr(constants, "ORDER_PART_CANCEL", 53)),
        }:
            return "CANCELLED"
        if raw_status == int(getattr(constants, "ORDER_JUNK", 57)):
            return "REJECTED"
        if raw_status in {
            int(getattr(constants, "ORDER_UNREPORTED", 48)),
            int(getattr(constants, "ORDER_WAIT_REPORTING", 49)),
            int(getattr(constants, "ORDER_REPORTED", 50)),
            int(getattr(constants, "ORDER_REPORTED_CANCEL", 51)),
            int(getattr(constants, "ORDER_PARTSUCC_CANCEL", 52)),
        }:
            return "SUBMITTED"
        return "UNKNOWN"

    @staticmethod
    def _load_sdk() -> tuple[Any, Any, Any, Any]:
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:
            raise QmtBrokerError(
                "未找到 xtquant；请从已授权的 QMT 客户端安装官方 SDK"
            ) from exc
        return XtQuantTrader, StockAccount, xtconstant, None
