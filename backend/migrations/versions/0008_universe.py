"""股票池快照、数据源健康、扩展证券字段（交易所、退市、状态、行业）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 扩展 securities 表（增加 universe 需要的字段，全部 nullable/server_default 兼容老行）
    with op.batch_alter_table("securities") as batch:
        batch.add_column(
            sa.Column(
                "exchange",
                sa.String(length=8),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column("delisted_date", sa.Date(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "trading_status",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            )
        )
        batch.add_column(
            sa.Column("sector", sa.String(length=50), nullable=True)
        )

    # 2. 股票池快照
    op.create_table(
        "universe_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("included_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source_provider",
            sa.String(length=32),
            nullable=False,
            server_default="mock",
        ),
        sa.Column("source_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("trading_day", name="uq_universe_snapshot_trading_day"),
    )
    op.create_index(
        "ix_universe_snapshot_trading_day",
        "universe_snapshots",
        ["trading_day"],
    )

    # 3. 快照成员
    op.create_table(
        "universe_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("universe_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column(
            "security_id",
            sa.String(length=16),
            sa.ForeignKey("securities.symbol", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_included", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("exclude_reason", sa.String(length=50), nullable=True),
        sa.Column("sort_rank", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "snapshot_id", "symbol", name="uq_universe_member_snapshot_symbol"
        ),
    )
    op.create_index(
        "ix_universe_member_snapshot_included",
        "universe_members",
        ["snapshot_id", "is_included"],
    )

    # 4. 数据源健康
    op.create_table(
        "data_source_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column(
            "last_status",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", name="uq_data_source_health_source_id"),
    )


def downgrade() -> None:
    op.drop_table("data_source_health")
    op.drop_index("ix_universe_member_snapshot_included", table_name="universe_members")
    op.drop_table("universe_members")
    op.drop_index("ix_universe_snapshot_trading_day", table_name="universe_snapshots")
    op.drop_table("universe_snapshots")
    with op.batch_alter_table("securities") as batch:
        batch.drop_column("sector")
        batch.drop_column("trading_status")
        batch.drop_column("delisted_date")
        batch.drop_column("exchange")
