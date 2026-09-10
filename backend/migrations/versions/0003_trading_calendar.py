"""新增交易日历表。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trading_calendar",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("trade_date"),
    )


def downgrade() -> None:
    op.drop_table("trading_calendar")
