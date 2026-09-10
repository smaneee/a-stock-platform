"""为回测任务增加可恢复字段（进度、幂等键、时间戳、错误摘要）并迁移状态值。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backtests") as batch_op:
        batch_op.add_column(sa.Column("progress", sa.Integer(), nullable=True, server_default="0"))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
        batch_op.create_unique_constraint("uq_backtest_idempotency", ["idempotency_key"])

    # 迁移旧状态值：PENDING→queued，DONE→succeeded
    op.execute("UPDATE backtests SET status='queued' WHERE status='PENDING'")
    op.execute("UPDATE backtests SET status='succeeded' WHERE status='DONE'")


def downgrade() -> None:
    op.execute("UPDATE backtests SET status='PENDING' WHERE status='queued'")
    op.execute("UPDATE backtests SET status='DONE' WHERE status='succeeded'")

    with op.batch_alter_table("backtests") as batch_op:
        batch_op.drop_constraint("uq_backtest_idempotency", type_="unique")
        batch_op.drop_column("finished_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("error_message")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("progress")
