"""Add recoverable daily pipeline runs.

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("paper_account_id", sa.Integer(), nullable=True),
        sa.Column("history_task_id", sa.Integer(), nullable=True),
        sa.Column("selection_run_id", sa.Integer(), nullable=True),
        sa.Column("paper_plan_id", sa.Integer(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("auto_execute_paper", sa.Boolean(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_account_id"], ["paper_accounts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["history_task_id"], ["history_ingest_batches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["selection_run_id"], ["selection_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["paper_plan_id"], ["paper_rebalance_plans.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "trading_day", "paper_account_id", name="uq_daily_pipeline_day_account"
        ),
    )
    op.create_index(
        "ix_daily_pipeline_status_stage",
        "daily_pipeline_runs",
        ["status", "stage"],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_pipeline_status_stage", table_name="daily_pipeline_runs")
    op.drop_table("daily_pipeline_runs")
