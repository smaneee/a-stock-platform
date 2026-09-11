"""Add reproducible selection runs and factor snapshots.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "selection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("universe_snapshots.id", ondelete="NO ACTION"),
            nullable=False,
        ),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("total_candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "snapshot_id", "config_hash", name="uq_selection_run_config"
        ),
    )
    op.create_index(
        "ix_selection_runs_trading_day_created",
        "selection_runs",
        ["trading_day", "created_at"],
    )
    op.create_table(
        "selection_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("selection_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("exchange", sa.String(length=8), nullable=False),
        sa.Column("board", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("momentum_20", sa.Float(), nullable=False),
        sa.Column("momentum_60", sa.Float(), nullable=False),
        sa.Column("volatility_20", sa.Float(), nullable=False),
        sa.Column("max_drawdown_60", sa.Float(), nullable=False),
        sa.Column("average_amount_20", sa.Float(), nullable=False),
        sa.Column("last_price", sa.Float(), nullable=False),
        sa.Column("bar_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("run_id", "symbol", name="uq_selection_candidate_symbol"),
        sa.UniqueConstraint("run_id", "rank", name="uq_selection_candidate_rank"),
    )
    op.create_index(
        "ix_selection_candidates_run_id", "selection_candidates", ["run_id"]
    )
    op.create_index(
        "ix_selection_candidates_symbol", "selection_candidates", ["symbol"]
    )


def downgrade() -> None:
    op.drop_index("ix_selection_candidates_symbol", table_name="selection_candidates")
    op.drop_index("ix_selection_candidates_run_id", table_name="selection_candidates")
    op.drop_table("selection_candidates")
    op.drop_index("ix_selection_runs_trading_day_created", table_name="selection_runs")
    op.drop_table("selection_runs")
