"""为模拟成交增加数据库级信号幂等约束。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("paper_trades") as batch_op:
        batch_op.create_unique_constraint(
            "uq_paper_trade_account_signal", ["account_id", "signal_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_trades") as batch_op:
        batch_op.drop_constraint("uq_paper_trade_account_signal", type_="unique")
