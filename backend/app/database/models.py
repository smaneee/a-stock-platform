"""ORM 数据模型定义。

覆盖自选股、信号、策略、模拟账户、委托、成交、回测等核心实体。
所有字段使用数据库无关的 SQLAlchemy 类型，兼容 SQLite 与 PostgreSQL。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
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
    """模拟账户持仓。

    持仓按买入批次拆分：quantity + acquisition_date + id 一一对应，
    用于精细化的 T+1 解冻（卖出时只允许 acquisition_date 早于当前交易日或
    已经 settle_t1 解冻的批次）。

    同一账户同一证券可以有多个批次（不强制唯一），用于支持同日多次买入
    与 FIFO 卖出平账。
    """

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    acquisition_date: Mapped[date | None] = mapped_column(Date, nullable=True)

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
    status: Mapped[str] = mapped_column(String(20), default="SUBMITTED")  # SUBMITTED/FILLED/CANCELLED/REJECTED
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PaperRebalancePlan(Base):
    """User-reviewable bridge from a selection run to paper orders."""

    __tablename__ = "paper_rebalance_plans"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "selection_run_id", name="uq_paper_rebalance_account_run"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    selection_run_id: Mapped[int] = mapped_column(
        ForeignKey("selection_runs.id", ondelete="NO ACTION"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    target_investment_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    max_symbol_weight: Mapped[float] = mapped_column(Float, nullable=False)
    validation_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proposal_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LiveRebalancePlan(Base):
    """One-time-approved plan for a configured local QMT account."""

    __tablename__ = "live_rebalance_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selection_run_id: Mapped[int] = mapped_column(
        ForeignKey("selection_runs.id", ondelete="NO ACTION"), nullable=False
    )
    account_fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    account_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    approval_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    execution_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DailyPipelineRun(Base):
    """Recoverable daily research-to-paper pipeline run."""

    __tablename__ = "daily_pipeline_runs"
    __table_args__ = (
        UniqueConstraint(
            "trading_day", "paper_account_id", name="uq_daily_pipeline_day_account"
        ),
        Index("ix_daily_pipeline_status_stage", "status", "stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    paper_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="SET NULL"), nullable=True
    )
    history_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("history_ingest_batches.id", ondelete="SET NULL"), nullable=True
    )
    selection_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("selection_runs.id", ondelete="SET NULL"), nullable=True
    )
    paper_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_rebalance_plans.id", ondelete="SET NULL"), nullable=True
    )
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    auto_execute_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


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
    """历史回测任务（可恢复，落库）。"""

    __tablename__ = "backtests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_backtest_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/succeeded/failed/cancelled
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 序列化的结果
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # 错误摘要
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class Security(Base):
    """股票主数据（名称、板块、ST 状态、上市日期）。

    由 SecurityMasterService 与 UniverseSyncService 共同读写，回测 / 组合 /
    模拟交易必须经此表解析股票基础信息，禁止依赖调用方临时传参。
    """

    __tablename__ = "securities"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    board: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    # 交易所: sh / sz / bj（衍生自 symbol 前缀，但显式存储以便查询）
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # trading_status: active / suspended / delisted
    trading_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    # 行业（可空，AKShare / Tushare 同步时填）
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    members: Mapped[list["UniverseMember"]] = relationship(  # noqa: F821
        back_populates="security",
        # 重要：禁止 delete-orphan / cascade 删历史快照。
        # 删除 Security 必须显式清理 UniverseMember；ORM 不会自动级联。
        cascade="save-update, merge",
        passive_deletes=True,
    )


class UniverseSnapshot(Base):
    """某一交易日的全市场股票池快照。

    关键设计：所有 selection / backtest / paper trading 必须基于某个
    UniverseSnapshot 查成分，禁止在回测中调用「当前实时」数据，避免
    未来数据与幸存者偏差。快照按 trading_day 唯一，便于跨日比对。
    """

    __tablename__ = "universe_snapshots"
    __table_args__ = (
        UniqueConstraint("trading_day", name="uq_universe_snapshot_trading_day"),
        Index("ix_universe_snapshot_trading_day", "trading_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    included_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="mock"
    )
    source_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    members: Mapped[list["UniverseMember"]] = relationship(  # noqa: F821
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="UniverseMember.sort_rank",
    )


class UniverseMember(Base):
    """Snapshot 与 Security 的关联行，包含排除原因。

    一只股票在某一快照只有一行：要么 is_included=True，要么 is_included=False
    且 exclude_reason 描述为什么被排除。即使被排除也必须落库（用于审计/重放）。

    重要：业务字段（name / exchange / sector / is_st / listing_date /
    delisted_date / trading_status）在 snapshot 创建时**一次性从 Security 拷贝**，
    形成不可变快照（immutable snapshot）。后续即便 Security 表的对应行被修改
    或删除，本行的字段也不会变。这是 point-in-time 查询的硬性保证。

    FK 行为：security_id 外键的 ondelete=NO ACTION（迁移 0009 修改），
    阻止删除被引用的 Security 时静默清空历史 UniverseMember。
    """

    __tablename__ = "universe_members"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "symbol", name="uq_universe_member_snapshot_symbol"
        ),
        Index("ix_universe_member_snapshot_included", "snapshot_id", "is_included"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("universe_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.symbol", ondelete="NO ACTION"), nullable=False
    )
    is_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exclude_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ───────────── 不可变业务字段（迁移 0009 新增） ─────────────
    # 这些字段在 snapshot 时从 Security 拷贝，断绝 JOIN 漂移。
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # board 不可变字段（迁移 0010 新增）：main / gem / star / bj / b_share / unknown
    board: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_st: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    trading_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default="active"
    )
    # 审计标签：incomplete_history / provider_unknown / ok
    # 不影响 is_included；selection / paper trading 可按需过滤
    audit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    snapshot: Mapped[UniverseSnapshot] = relationship(back_populates="members")
    security: Mapped[Security] = relationship(back_populates="members")


class DataSourceHealth(Base):
    """数据源健康状况记录（最新一次成功 / 失败状态）。

    用于 universe_sync_service 实时报告哪些 Provider 在线，
    以及为选择逻辑记录：上一次同步是哪条数据源完成的。
    """

    __tablename__ = "data_source_health"
    __table_args__ = (
        UniqueConstraint("source_id", name="uq_data_source_health_source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class HistoryIngestBatch(Base):
    """历史数据 ingest 批次记录。

    记录每次历史数据拉取的覆盖范围（start/end + 实际拉到的 symbol 数 + 完成的 bar 数）。
    long_suspension 判定必须以「最近一次成功的 ingest 批次覆盖」为前提：
    没有覆盖就只能是 incomplete_history，不能判 confirmed。
    """

    __tablename__ = "history_ingest_batches"
    __table_args__ = (
        Index("ix_history_ingest_batches_status_completed", "status", "completed_at"),
        Index("ix_history_ingest_batches_source_completed", "source", "completed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("universe_snapshots.id", ondelete="NO ACTION"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # provider_manager / mock
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running"
    )  # queued / running / succeeded / partial / failed / cancelled
    adjust: Mapped[str] = mapped_column(String(8), nullable=False, default="none")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_symbols: Mapped[int] = mapped_column(Integer, default=0)
    completed_symbols: Mapped[int] = mapped_column(Integer, default=0)
    total_bars: Mapped[int] = mapped_column(Integer, default=0)
    coverage_ratio: Mapped[float] = mapped_column(
        Float, default=0.0
    )  # completed_symbols / requested_symbols
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 拉到的 symbol 列表（JSON 数组），用于 long_suspension 覆盖检查
    covered_symbols: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # queued 任务必须持久化请求集合，服务重启后才能恢复执行
    requested_symbol_list: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # symbol -> 简短错误摘要，便于局部重试与前端诊断
    failed_symbols: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SelectionRun(Base):
    """一次可复现的截面选股运行。"""

    __tablename__ = "selection_runs"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "config_hash", name="uq_selection_run_config"),
        Index("ix_selection_runs_trading_day_created", "trading_day", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("universe_snapshots.id", ondelete="NO ACTION"), nullable=False
    )
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    total_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluation_horizon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluation_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_forward_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_forward_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    candidates: Mapped[list["SelectionCandidate"]] = relationship(  # noqa: F821
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="SelectionCandidate.rank",
    )


class SelectionCandidate(Base):
    """选股运行输出的不可变候选与因子快照。"""

    __tablename__ = "selection_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", name="uq_selection_candidate_symbol"),
        UniqueConstraint("run_id", "rank", name="uq_selection_candidate_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("selection_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    board: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    momentum_20: Mapped[float] = mapped_column(Float, nullable=False)
    momentum_60: Mapped[float] = mapped_column(Float, nullable=False)
    volatility_20: Mapped[float] = mapped_column(Float, nullable=False)
    max_drawdown_60: Mapped[float] = mapped_column(Float, nullable=False)
    average_amount_20: Mapped[float] = mapped_column(Float, nullable=False)
    last_price: Mapped[float] = mapped_column(Float, nullable=False)
    bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    forward_return: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[SelectionRun] = relationship(back_populates="candidates")


class PortfolioBacktest(Base):
    """组合回测任务（可恢复，落库）。"""

    __tablename__ = "portfolio_backtests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_portfolio_backtest_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbols: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 数组
    weights: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 对象
    benchmark_symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    max_single_position: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0.2"))
    max_total_position: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0.95"))
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=Decimal("0.0003"))
    slippage: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=Decimal("0.0005"))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 序列化的结果
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class DailySettlementRecord(Base):
    """每日日终结算记录，按 (account_id, trading_date) 唯一。"""

    __tablename__ = "daily_settlements"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "trading_date", name="uq_settlement_account_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_asset: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    positions_settled: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
