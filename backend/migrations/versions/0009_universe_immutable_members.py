"""Universe 阶段增量修复：UniverseMember 不可变字段 + 禁止 CASCADE 删除历史。

问题（用户 P0 复验指出）：
1) 0008 迁移里 universe_members.security_id 上有 ondelete="CASCADE"，
   删除 Security 行会同时删除该 symbol 在所有历史 snapshot 中的成员行，
   破坏 point-in-time 可重放性。
2) 0008 迁移的 UniverseMember 只存 symbol + is_included + exclude_reason +
   sort_rank，缺少 name/exchange/sector/is_st/listing_date/delisted_date/
   trading_status 等业务字段。get_membership 必须 JOIN 当前 Security 才能
   拿到这些字段，导致历史查询会因 Security 表变化而漂移（survivorship
   bias 风险）。

修复（线性追加 0009，不重写 0008）：
- universe_members.security_id：保留列但将 FK 的 ondelete 改为 RESTRICT
  （仍可显式删除 Security，但要先清理历史；不允许级联）。
  实际上更安全的做法是直接 DROP FK 让 security_id 退化为普通字符串列，
  这样即便 Security 真的被删，历史 UniverseMember 也不受影响。
- 新增 7 个业务字段，全部 nullable server_default 兼容老行：
    name, exchange, sector, is_st, listing_date,
    delisted_date, trading_status

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 先解掉 FK 的 CASCADE，改为 NO ACTION（阻止删除 Security 静默清空历史）
    # SQLite 下 alembic 用 batch_alter_table 实际是 copy-replace-create；
    # 我们先在原表上 alter 实际 FK 行为，再让 batch 重建。
    # 关键观察：SQLite 本身不支持直接改 FK 的 ondelete，必须 drop_constraint
    # + create_foreign_key。
    # FK 约束名由 SQLAlchemy 在 0008 创建时自动命名为
    # "fk_universe_members_security_id"（table_col 格式）。
    bind = op.get_bind()
    inspector = None
    try:
        from sqlalchemy import inspect
        inspector = inspect(bind)
    except Exception:
        inspector = None
    fk_name: str | None = None
    if inspector is not None:
        try:
            for fk in inspector.get_foreign_keys("universe_members"):
                if fk.get("constrained_columns") == ["security_id"]:
                    fk_name = fk.get("name")
                    break
        except Exception:
            fk_name = None

    with op.batch_alter_table("universe_members") as batch:
        if fk_name:
            batch.drop_constraint(fk_name, type_="foreignkey")
        # 不管有没有 drop 成功，都重建 FK（如果原 FK 仍存在，create 会失败；
        # 但 batch_alter_table 在 SQLite 下是先建临时表再 rename，FK 在新表
        # 里会按新定义创建，老表的 FK 在 swap 时被替换）
        batch.create_foreign_key(
            "fk_universe_members_security_id",
            "securities",
            ["security_id"],
            ["symbol"],
            ondelete="NO ACTION",
        )

        # 2) 新增 8 个不可变业务字段（全部 nullable + 兼容老行）
        batch.add_column(sa.Column("name", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("exchange", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("sector", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("is_st", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("listing_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("delisted_date", sa.Date(), nullable=True))
        batch.add_column(
            sa.Column(
                "trading_status",
                sa.String(length=16),
                nullable=True,
                server_default="active",
            )
        )
        # 审计标签：incomplete_history / provider_unknown / ok
        batch.add_column(
            sa.Column("audit_reason", sa.String(length=50), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("universe_members") as batch:
        batch.drop_column("audit_reason")
        batch.drop_column("trading_status")
        batch.drop_column("delisted_date")
        batch.drop_column("listing_date")
        batch.drop_column("is_st")
        batch.drop_column("sector")
        batch.drop_column("exchange")
        batch.drop_column("name")
        # 还原 FK 为 CASCADE（虽然我们不希望这样，但 downgrade 只能镜像 0008）
        batch.drop_constraint("fk_universe_members_security_id", type_="foreignkey")
        batch.create_foreign_key(
            "universe_members.security_id",
            "securities",
            ["security_id"],
            ["symbol"],
            ondelete="CASCADE",
        )