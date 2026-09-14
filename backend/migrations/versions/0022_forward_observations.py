"""Add forward_observations table (P1-02 前向模拟观察计时).

背景：研发计划要求「工程冻结后累计前向模拟交易日；重大成交逻辑修改后重新计时」，
但原库中**没有任何表或字段记录前向交易日**，无法审计「60 个交易日」这类结论。

本迁移只新增一张表，不修改任何既有表或数据；回滚即删除该表。

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forward_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("freeze_tag", sa.String(length=64), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("target_trading_days", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("trading_days_counted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_counted_day", sa.Date(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("baseline_equity", sa.Float(), nullable=True),
        sa.Column("current_equity", sa.Float(), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("freeze_tag", name="uq_forward_observation_freeze_tag"),
    )
    op.create_index(
        "ix_forward_observation_started_on", "forward_observations", ["started_on"]
    )


def downgrade() -> None:
    op.drop_index("ix_forward_observation_started_on", table_name="forward_observations")
    op.drop_table("forward_observations")
