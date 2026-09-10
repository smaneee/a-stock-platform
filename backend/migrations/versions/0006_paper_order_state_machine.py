"""模拟交易订单状态机与现金冻结。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("paper_orders") as batch_op:
        batch_op.add_column(sa.Column("reject_reason", sa.Text(), nullable=True))

    with op.batch_alter_table("paper_trades") as batch_op:
        batch_op.add_column(
            sa.Column("realized_pnl", sa.Numeric(16, 2), nullable=True, server_default="0")
        )

    with op.batch_alter_table("paper_positions") as batch_op:
        batch_op.add_column(
            sa.Column("realized_pnl", sa.Numeric(16, 2), nullable=True, server_default="0")
        )

    # 旧状态 PENDING 迁移为 SUBMITTED
    op.execute("UPDATE paper_orders SET status='SUBMITTED' WHERE status='PENDING'")


def downgrade() -> None:
    op.execute("UPDATE paper_orders SET status='PENDING' WHERE status='SUBMITTED'")

    with op.batch_alter_table("paper_positions") as batch_op:
        batch_op.drop_column("realized_pnl")
    with op.batch_alter_table("paper_trades") as batch_op:
        batch_op.drop_column("realized_pnl")
    with op.batch_alter_table("paper_orders") as batch_op:
        batch_op.drop_column("reject_reason")
