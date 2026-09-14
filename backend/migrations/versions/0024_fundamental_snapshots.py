"""Add fundamental_snapshots (投资决策辅助：基本面/估值快照).

背景：全库实测 28 张表**没有任何财务或估值数据**（PE/PB/ROE/营收/净利润均无处存放），
因此「企业质量 / 估值 / 市场预期」三类分析无法开展。本迁移新建
`fundamental_snapshots` 表承载**已验证字段**。

字段语义的验证方法见 `app/fundamentals/eastmoney_fundamentals.py` 模块文档
（用 akshare 的独立财务摘要交叉核对东财行情接口字段，逐项数值一致）。

要点：
* 唯一键 `(symbol, snapshot_date, source)` → 同日重复抓取是更新而非追加；
* `report_date` 单独存 → 分析时必须能判断财务数据时效（过期须停止判断）；
* 未验证的接口字段**不入库**（宁缺毋滥）；
* 回滚即删表。

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fundamental_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("float_market_cap", sa.Float(), nullable=True),
        sa.Column("pe_dynamic", sa.Float(), nullable=True),
        sa.Column("pb", sa.Float(), nullable=True),
        sa.Column("roe", sa.Float(), nullable=True),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("revenue_yoy", sa.Float(), nullable=True),
        sa.Column("net_profit_parent", sa.Float(), nullable=True),
        sa.Column("net_profit_yoy", sa.Float(), nullable=True),
        sa.Column("gross_margin", sa.Float(), nullable=True),
        sa.Column("net_margin", sa.Float(), nullable=True),
        sa.Column("debt_ratio", sa.Float(), nullable=True),
        sa.Column("eps_diluted", sa.Float(), nullable=True),
        sa.Column("bps", sa.Float(), nullable=True),
        sa.Column("equity", sa.Float(), nullable=True),
        sa.Column("industry", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="eastmoney"),
        sa.Column("warnings", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "symbol", "snapshot_date", "source", name="uq_fundamental_symbol_date_source"
        ),
    )
    op.create_index(
        "ix_fundamental_snapshot_date", "fundamental_snapshots", ["snapshot_date"]
    )
    op.create_index(
        "ix_fundamental_report_date", "fundamental_snapshots", ["report_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_fundamental_report_date", table_name="fundamental_snapshots")
    op.drop_index("ix_fundamental_snapshot_date", table_name="fundamental_snapshots")
    op.drop_table("fundamental_snapshots")
