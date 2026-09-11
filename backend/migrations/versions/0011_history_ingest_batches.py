"""Universe 完整性：新增 history_ingest_batches 表。

记录历史数据 ingest 批次，用于 long_suspension 覆盖检查。

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "history_ingest_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("requested_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_bars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "coverage_ratio",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("covered_symbols", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_history_ingest_batches_status_completed",
        "history_ingest_batches",
        ["status", "completed_at"],
    )
    op.create_index(
        "ix_history_ingest_batches_source_completed",
        "history_ingest_batches",
        ["source", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_history_ingest_batches_source_completed", table_name="history_ingest_batches"
    )
    op.drop_index(
        "ix_history_ingest_batches_status_completed", table_name="history_ingest_batches"
    )
    op.drop_table("history_ingest_batches")