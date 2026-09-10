"""ORM 数据模型定义。

覆盖自选股、信号、策略、模拟账户、委托、成交、回测等核心实体。
所有字段使用数据库无关的 SQLAlchemy 类型，兼容 SQLite 与 PostgreSQL。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base
from app.time_utils import utc_now


class Watchlist(Base):
    """自选股列表。"""

    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    symbols: Mapped[list["WatchlistSymbol"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )


class WatchlistSymbol(Base):
    """自选股列表中的某只股票。"""

    __tablename__ = "watchlist_symbols"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    watchlist: Mapped["Watchlist"] = relationship(back_populates="symbols")


class Strategy(Base):
    """策略定义与启用状态。"""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str] = mapped_column(String(20), default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Signal(Base):
    """策略信号记录。"""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY / SELL / ALERT
    strength: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=1.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    source_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    strategy_version: Mapped[str] = mapped_column(String(20), default="1.0.0")


class PaperAccount(Base):
    """模拟交易账户。"""

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    available_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    frozen_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    positions: Mapped[list["PaperPosition"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class PaperPosition(Base):
    """模拟账户持仓。"""

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_position_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)

    account: Mapped["PaperAccount"] = relationship(back_populates="positions")


class PaperOrder(Base):
    """模拟委托单。"""

    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY / SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING/FILLED/CANCELLED/REJECTED
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PaperTrade(Base):
    """模拟成交记录。"""

    __tablename__ = "paper_trades"
    __table_args__ = (
        UniqueConstraint("account_id", "signal_id", name="uq_paper_trade_account_signal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    stamp_tax: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AssetRecord(Base):
    """模拟账户资产曲线记录，用于计算回撤与资产曲线。"""

    __tablename__ = "asset_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    total_asset: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Backtest(Base):
    """历史回测任务。"""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING/RUNNING/DONE/FAILED
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 序列化的结果
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class TradingDate(Base):
    """交易日历。只存储交易日（节假日不落库）。"""

    __tablename__ = "trading_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)


class HistoricalBar(Base):
    """历史 K 线（本地缓存，供回测使用）。

    以 (symbol, period, adjust, trade_date) 唯一，支持增量同步与多复权方式并存。
    """

    __tablename__ = "historical_bars"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "period", "adjust", "trade_date", name="uq_historical_bar"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(10), nullable=False, default="daily")
    adjust: Mapped[str] = mapped_column(String(10), nullable=False, default="none")  # none/qfq/hfq
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    high: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    low: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    close: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    volume: Mapped[float] = mapped_column(Float, default=0)  # 成交量（单位与数据源一致）
    amount: Mapped[float] = mapped_column(Float, default=0)  # 成交额（元）
    source: Mapped[str] = mapped_column(String(20), default="akshare")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
