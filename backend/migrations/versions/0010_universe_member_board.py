"""Universe 完整性：UniverseMember 新增 board 不可变字段。

问题：0008/0009 的 UniverseMember 缺 board 字段，get_membership 拿不到当时板块
信息（主板/创业板/科创板/北证），需要 JOIN 当前 Security 才能补，而 Security 表
可能已被改 / 删（point-in-time 失效）。

修复：线性追加 0010，给 UniverseMember 加 board 不可变字段；snapshot 时从
SecurityRecord.board 拷贝（已由 provider 按代码前缀推断并写入）。

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("universe_members") as batch:
        batch.add_column(
            sa.Column("board", sa.String(length=16), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("universe_members") as batch:
        batch.drop_column("board")