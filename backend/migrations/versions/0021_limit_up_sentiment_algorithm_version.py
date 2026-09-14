"""Add algorithm_version to limit-up sentiment.

研发计划 P0-03 要求涨停情绪池保存 ``source`` / ``captured_at`` /
``coverage_symbols`` / **算法版本**。前三个字段已存在（0019/0020），本迁移补齐
算法版本，用于区分：

* ``eastmoney-push2ex-v1``：东财涨停板榜单实抓（封板资金、封板时间、连板数来自上游）
* ``derived-daily-high-close-v1``：本地不复权日线 high/close 近似回算（无法还原
  封板次数、封板时间与盘口队列）

没有版本号时，两种来源的历史曲线会被当成同一口径比较，属不可审计状态。

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "limit_up_sentiment",
        sa.Column("algorithm_version", sa.String(length=32), nullable=True),
    )
    # 历史行按已知来源回填版本，避免新旧数据混在一起无法区分
    op.execute(
        "UPDATE limit_up_sentiment SET algorithm_version = 'eastmoney-push2ex-v1' "
        "WHERE source = 'eastmoney' AND algorithm_version IS NULL"
    )
    op.execute(
        "UPDATE limit_up_sentiment SET algorithm_version = 'derived-daily-high-close-v1' "
        "WHERE source = 'derived' AND algorithm_version IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("limit_up_sentiment") as batch:
        batch.drop_column("algorithm_version")
