"""Add limit-up sentiment factor tables.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "limit_up_sentiment",
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("limit_up_count", sa.Integer(), nullable=False),
        sa.Column("limit_down_count", sa.Integer(), nullable=False),
        sa.Column("broken_board_count", sa.Integer(), nullable=False),
        sa.Column("strong_count", sa.Integer(), nullable=False),
        sa.Column("sub_new_count", sa.Integer(), nullable=False),
        sa.Column("seal_rate", sa.Float(), nullable=True),
        sa.Column("broken_rate", sa.Float(), nullable=True),
        sa.Column("max_streak", sa.Integer(), nullable=False),
        sa.Column("first_board_count", sa.Integer(), nullable=False),
        sa.Column("streak_2_count", sa.Integer(), nullable=False),
        sa.Column("streak_3_count", sa.Integer(), nullable=False),
        sa.Column("streak_4_count", sa.Integer(), nullable=False),
        sa.Column("streak_5plus_count", sa.Integer(), nullable=False),
        sa.Column("total_seal_amount", sa.Float(), nullable=False),
        sa.Column("total_limit_up_amount", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "limit_up_pool_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("pool", sa.String(length=24), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("change_pct", sa.Float(), nullable=True),
        sa.Column("limit_up_price", sa.Float(), nullable=True),
        sa.Column("seal_amount", sa.Float(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("turnover_rate", sa.Float(), nullable=True),
        sa.Column("boards", sa.Integer(), nullable=True),
        sa.Column("first_seal_time", sa.String(length=12), nullable=False),
        sa.Column("last_seal_time", sa.String(length=12), nullable=False),
        sa.Column("limit_up_stat", sa.String(length=24), nullable=False),
        sa.Column("industry", sa.String(length=50), nullable=False),
        sa.Column("is_new_high", sa.Boolean(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "trade_date", "pool", "symbol", name="uq_limit_up_pool_member"
        ),
    )
    op.create_index(
        "ix_limit_up_member_date_pool",
        "limit_up_pool_members",
        ["trade_date", "pool"],
    )
    op.create_index(
        "ix_limit_up_pool_members_trade_date",
        "limit_up_pool_members",
        ["trade_date"],
    )
    op.create_index(
        "ix_limit_up_pool_members_symbol", "limit_up_pool_members", ["symbol"]
    )


def downgrade() -> None:
    op.drop_index("ix_limit_up_pool_members_symbol", table_name="limit_up_pool_members")
    op.drop_index(
        "ix_limit_up_pool_members_trade_date", table_name="limit_up_pool_members"
    )
    op.drop_index("ix_limit_up_member_date_pool", table_name="limit_up_pool_members")
    op.drop_table("limit_up_pool_members")
    op.drop_table("limit_up_sentiment")
