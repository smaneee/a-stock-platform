"""Add out-of-sample forward evaluation fields.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("selection_runs") as batch:
        batch.add_column(sa.Column("evaluation_horizon", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("evaluation_coverage", sa.Float(), nullable=True))
        batch.add_column(sa.Column("mean_forward_return", sa.Float(), nullable=True))
        batch.add_column(sa.Column("median_forward_return", sa.Float(), nullable=True))
        batch.add_column(sa.Column("forward_win_rate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("evaluated_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("selection_candidates") as batch:
        batch.add_column(sa.Column("entry_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("exit_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("forward_return", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("selection_candidates") as batch:
        batch.drop_column("forward_return")
        batch.drop_column("exit_date")
        batch.drop_column("entry_date")
    with op.batch_alter_table("selection_runs") as batch:
        batch.drop_column("evaluated_at")
        batch.drop_column("forward_win_rate")
        batch.drop_column("median_forward_return")
        batch.drop_column("mean_forward_return")
        batch.drop_column("evaluation_coverage")
        batch.drop_column("evaluation_horizon")
