"""Add coverage_symbols to limit-up sentiment.

回算口径（``app.market_data.sentiment_backfill``）需要记录当日纳入统计的
标的数，才能说明封板率 / 连板高度是在多大的样本面上算出来的。

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "limit_up_sentiment",
        sa.Column("coverage_symbols", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("limit_up_sentiment") as batch:
        batch.drop_column("coverage_symbols")
