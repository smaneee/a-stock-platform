"""新增历史行情本地缓存表。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_bars",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("adjust", sa.String(length=10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(12, 4), nullable=True),
        sa.Column("high", sa.Numeric(12, 4), nullable=True),
        sa.Column("low", sa.Numeric(12, 4), nullable=True),
        sa.Column("close", sa.Numeric(12, 4), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol", "period", "adjust", "trade_date", name="uq_historical_bar"
        ),
    )
    op.create_index(
        "ix_historical_bars_symbol", "historical_bars", ["symbol"], unique=False
    )
    op.create_index(
        "ix_historical_bars_trade_date", "historical_bars", ["trade_date"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_historical_bars_trade_date", table_name="historical_bars")
    op.drop_index("ix_historical_bars_symbol", table_name="historical_bars")
    op.drop_table("historical_bars")
