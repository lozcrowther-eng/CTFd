"""Add run_token to mayfly_instances

Revision ID: b7d4a1f9c3e6
Revises: a1c9f3e7b02d
Create Date: 2026-09-23 00:00:00.000000

Only reached on a non-SQLite database, same as a1c9f3e7b02d (CTFd.plugins.migrations.upgrade()
calls db.create_all() directly for SQLite and never looks at this directory) -- see
__init__.py's _ensure_run_token_column() for how an already-running SQLite deployment (this
project's Cloud Run instance) picks up the same column.
"""
import sqlalchemy as sa

from CTFd.plugins.migrations import get_columns_for_table

revision = "b7d4a1f9c3e6"
down_revision = "a1c9f3e7b02d"
branch_labels = None
depends_on = None


def upgrade(op=None):
    columns = get_columns_for_table(op, "mayfly_instances", names_only=True)
    if "run_token" not in columns:
        op.add_column("mayfly_instances", sa.Column("run_token", sa.String(length=128), nullable=True))


def downgrade(op=None):
    columns = get_columns_for_table(op, "mayfly_instances", names_only=True)
    if "run_token" in columns:
        op.drop_column("mayfly_instances", "run_token")
