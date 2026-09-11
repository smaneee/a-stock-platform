"""Normalize the legacy universe_members foreign-key definition.

Revision ID: 0017
Revises: 0016
"""
import warnings

from alembic import op
from sqlalchemy.exc import SAWarning


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


_LEGACY_WARNING = (
    r"WARNING: SQL-parsed foreign key constraint .* could not be located "
    r"in PRAGMA foreign_keys for table universe_members"
)
_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}


def _rebuild_with_single_reflected_fk() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=_LEGACY_WARNING, category=SAWarning)
        with op.batch_alter_table(
            "universe_members",
            recreate="always",
            naming_convention=_NAMING_CONVENTION,
        ):
            pass


def upgrade() -> None:
    _rebuild_with_single_reflected_fk()


def downgrade() -> None:
    _rebuild_with_single_reflected_fk()
