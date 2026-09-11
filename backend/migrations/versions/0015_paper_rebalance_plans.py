"""Add reviewable paper rebalance plans.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_rebalance_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("selection_run_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_investment_ratio", sa.Float(), nullable=False),
        sa.Column("max_symbol_weight", sa.Float(), nullable=False),
        sa.Column("validation_override", sa.Boolean(), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("execution_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["selection_run_id"], ["selection_runs.id"], ondelete="NO ACTION"),
        sa.UniqueConstraint("account_id", "selection_run_id", name="uq_paper_rebalance_account_run"),
    )
    op.create_index(
        "ix_paper_rebalance_plans_account_id",
        "paper_rebalance_plans",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_rebalance_plans_account_id",
        table_name="paper_rebalance_plans",
    )
    op.drop_table("paper_rebalance_plans")
