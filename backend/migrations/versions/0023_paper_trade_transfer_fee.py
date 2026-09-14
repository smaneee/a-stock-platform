"""Add paper_trades.transfer_fee (P1-02 过户费缺口).

背景：回测引擎按 0.001% **双边**计提过户费（`ExecutionConfig.transfer_fee_rate`），
而模拟盘的 `PaperTrade` 没有这一列、broker 也未计提 —— 两套成交假设不一致，
模拟盘成交成本**系统性偏低**（P1-02 第 9 节第 7 条登记项）。

本迁移只**新增一列**，不改任何既有列与数据：
* 列可空性：`nullable=True`（SQLite 加列必须允许 NULL 或给 server_default）；
* `server_default="0"` 保证历史行读到 0 而不是 NULL，避免下游 `float(None)` 崩；
* 回滚即删列。

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_trades",
        sa.Column(
            "transfer_fee",
            sa.Numeric(12, 4),
            nullable=True,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("paper_trades", "transfer_fee")
