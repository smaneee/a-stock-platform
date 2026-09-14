"""Add verified balance-sheet and cash-flow detail fields.

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


COLUMNS = (
    ("operating_cash_flow", sa.Float()),
    ("capital_expenditure", sa.Float()),
    ("monetary_funds", sa.Float()),
    ("short_loan", sa.Float()),
    ("long_loan", sa.Float()),
    ("bonds_payable", sa.Float()),
    ("noncurrent_liab_due_year", sa.Float()),
    ("lease_liabilities", sa.Float()),
    ("goodwill", sa.Float()),
    ("statement_equity", sa.Float()),
    ("statement_report_date", sa.Date()),
    ("statement_source", sa.String(length=40)),
    ("statement_fetched_at", sa.DateTime()),
)


def upgrade() -> None:
    with op.batch_alter_table("fundamental_snapshots") as batch:
        for name, column_type in COLUMNS:
            batch.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("fundamental_snapshots") as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
