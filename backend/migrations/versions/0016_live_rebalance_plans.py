"""Add explicitly approved QMT live rebalance plans.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_rebalance_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("selection_run_id", sa.Integer(), nullable=False),
        sa.Column("account_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("account_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("approval_token_hash", sa.String(length=64), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(), nullable=True),
        sa.Column("execution_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["selection_run_id"], ["selection_runs.id"], ondelete="NO ACTION"
        ),
    )


def downgrade() -> None:
    op.drop_table("live_rebalance_plans")
