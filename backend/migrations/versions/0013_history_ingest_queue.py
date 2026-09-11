"""Turn history ingest batches into recoverable background jobs.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("history_ingest_batches") as batch:
        batch.add_column(sa.Column("snapshot_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("adjust", sa.String(length=8), nullable=False, server_default="none")
        )
        batch.add_column(
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("requested_symbol_list", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("failed_symbols", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            "fk_history_ingest_snapshot",
            "universe_snapshots",
            ["snapshot_id"],
            ["id"],
            ondelete="NO ACTION",
        )


def downgrade() -> None:
    with op.batch_alter_table("history_ingest_batches") as batch:
        batch.drop_constraint("fk_history_ingest_snapshot", type_="foreignkey")
        batch.drop_column("failed_symbols")
        batch.drop_column("requested_symbol_list")
        batch.drop_column("cancel_requested")
        batch.drop_column("progress")
        batch.drop_column("adjust")
        batch.drop_column("snapshot_id")
