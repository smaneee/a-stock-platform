"""初始数据库迁移

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "watchlist_symbols",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_id", sa.Integer(), sa.ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_symbol"),
    )

    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("version", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("signal_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("strength", sa.Numeric(8, 4), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("source_time", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("strategy_version", sa.String(length=20), nullable=True),
    )
    op.create_index("ix_signals_signal_id", "signals", ["signal_id"])
    op.create_index("ix_signals_symbol", "signals", ["symbol"])

    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("initial_cash", sa.Numeric(16, 2), nullable=False),
        sa.Column("available_cash", sa.Numeric(16, 2), nullable=False),
        sa.Column("frozen_cash", sa.Numeric(16, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("available_quantity", sa.Integer(), nullable=False),
        sa.Column("avg_cost", sa.Numeric(12, 4), nullable=True),
        sa.UniqueConstraint("account_id", "symbol", name="uq_position_symbol"),
    )

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("commission", sa.Numeric(12, 4), nullable=True),
        sa.Column("stamp_tax", sa.Numeric(12, 4), nullable=True),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "asset_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("total_asset", sa.Numeric(16, 2), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "backtests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=False),
        sa.Column("initial_cash", sa.Numeric(16, 2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("backtests")
    op.drop_table("asset_records")
    op.drop_table("paper_trades")
    op.drop_table("paper_orders")
    op.drop_table("paper_positions")
    op.drop_table("paper_accounts")
    op.drop_table("signals")
    op.drop_table("strategies")
    op.drop_table("watchlist_symbols")
    op.drop_table("watchlists")
