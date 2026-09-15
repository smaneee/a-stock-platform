"""Add research_review_reminders (研究记录自动复核提醒).

背景：研究记录已能冻结并手动复核；本迁移让**收盘后自动扫描**把触发结果落库，
形成可确认、可审计的待办列表。

要点：
* 唯一键 `(run_id, trigger_name, detected_on)` → 同一天重复扫描是幂等，不产生重复提醒；
* `acknowledged` 只表示"人看过了"，不删除历史；
* 回滚即删表。

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_review_reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("trigger_name", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("severity", sa.String(length=12), nullable=False, server_default="medium"),
        sa.Column("detected_on", sa.Date(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["investment_research_runs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "run_id", "trigger_name", "detected_on", name="uq_reminder_run_trigger_day"
        ),
    )
    op.create_index("ix_reminder_detected_on", "research_review_reminders", ["detected_on"])
    op.create_index("ix_reminder_symbol", "research_review_reminders", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_reminder_symbol", table_name="research_review_reminders")
    op.drop_index("ix_reminder_detected_on", table_name="research_review_reminders")
    op.drop_table("research_review_reminders")
