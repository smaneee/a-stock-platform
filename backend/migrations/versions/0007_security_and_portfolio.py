"""证券主数据、组合回测任务、日终结算表与持仓批次日期。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 证券主数据表
    op.create_table(
        "securities",
        sa.Column("symbol", sa.String(length=16), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("board", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("is_st", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="manual"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # 组合回测任务表（含幂等键唯一约束，SQLite 友好：写在表里）
    op.create_table(
        "portfolio_backtests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbols", sa.Text(), nullable=False),
        sa.Column("weights", sa.Text(), nullable=True),
        sa.Column("benchmark_symbol", sa.String(length=16), nullable=True),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=False),
        sa.Column("initial_cash", sa.Numeric(16, 2), nullable=False),
        sa.Column(
            "max_single_position",
            sa.Numeric(8, 4),
            nullable=True,
            server_default="0.2",
        ),
        sa.Column(
            "max_total_position",
            sa.Numeric(8, 4),
            nullable=True,
            server_default="0.95",
        ),
        sa.Column(
            "commission_rate",
            sa.Numeric(8, 6),
            nullable=True,
            server_default="0.0003",
        ),
        sa.Column(
            "slippage",
            sa.Numeric(8, 6),
            nullable=True,
            server_default="0.0005",
        ),
        sa.Column("status", sa.String(length=20), server_default="queued"),
        sa.Column("progress", sa.Integer(), server_default="0"),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_portfolio_backtest_idempotency"
        ),
    )

    # 日终结算表（按 account_id, trading_date 唯一，account 外键级联删除）
    op.create_table(
        "daily_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("total_asset", sa.Numeric(16, 2), server_default="0"),
        sa.Column("positions_settled", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_accounts.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "account_id", "trading_date", name="uq_settlement_account_date"
        ),
    )

    # 持仓表：移除旧 symbol 唯一约束，新增 acquisition_date 列
    # 注意：持仓按批次拆分，同一账户同一证券可能有多个批次，因此不强制
    # (account_id, symbol, acquisition_date) 唯一约束。
    with op.batch_alter_table("paper_positions") as batch_op:
        batch_op.drop_constraint("uq_position_symbol", type_="unique")
        batch_op.add_column(
            sa.Column("acquisition_date", sa.Date(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_positions") as batch_op:
        batch_op.drop_column("acquisition_date")
        batch_op.create_unique_constraint(
            "uq_position_symbol", ["account_id", "symbol"]
        )
    op.drop_table("daily_settlements")
    op.drop_table("portfolio_backtests")
    op.drop_table("securities")
