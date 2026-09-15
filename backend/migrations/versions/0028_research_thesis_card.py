"""Add investment_research_runs.thesis_card (S1 结构化研究决策卡).

方案 `docs/DeepSeek-投资策略思考升级执行方案.md` §二要求输出结构化字段
（strategy_type / horizon / thesis / variant_view / 证据 id / 假设 / 估值方法 /
情景结果 id / 失效条件 / 复核触发 / 缺失数据 / 决策 / 置信依据 / 模型与提示词版本）。
本迁移把这些字段作为**冻结快照**存进研究记录，便于日后复盘与对比。

要点：
* 列可空（历史记录没有卡片，不许编造补写）；
* 回滚即删列。

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("investment_research_runs") as batch:
        batch.add_column(sa.Column("thesis_card", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("model_latency_ms", sa.Float(), nullable=True))
        batch.add_column(sa.Column("model_total_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("investment_research_runs") as batch:
        batch.drop_column("model_total_tokens")
        batch.drop_column("model_latency_ms")
        batch.drop_column("thesis_card")
