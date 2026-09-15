"""Add immutable investment research runs.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investment_research_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("conclusion_key", sa.String(length=40), nullable=False),
        sa.Column("explanation_status", sa.String(length=24), nullable=False, server_default="not_requested"),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=False),
        sa.Column("reverse_valuation", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_investment_research_symbol_created",
        "investment_research_runs",
        ["symbol", "created_at"],
    )
    op.create_index(
        "ix_investment_research_fingerprint",
        "investment_research_runs",
        ["fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_investment_research_fingerprint", table_name="investment_research_runs")
    op.drop_index("ix_investment_research_symbol_created", table_name="investment_research_runs")
    op.drop_table("investment_research_runs")
