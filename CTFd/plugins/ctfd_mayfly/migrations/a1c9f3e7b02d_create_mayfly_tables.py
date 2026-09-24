"""Create mayfly_challenges, mayfly_instances, and mayfly_flag_share_events tables

Revision ID: a1c9f3e7b02d
Revises:
Create Date: 2026-09-22 00:00:00.000000

Only reached on a non-SQLite database (CTFd.plugins.migrations.upgrade() calls
db.create_all() directly for SQLite and never even looks at this directory) — i.e. this is
what actually creates these tables on a real MySQL CTFd deployment. `op` is passed as a
keyword by CTFd's own plugin migration runner, not alembic's usual thread-local context —
see CTFd/plugins/migrations.py's `r.module.upgrade(op=op)`.
"""
import sqlalchemy as sa

from CTFd.plugins.migrations import get_all_tables

revision = "a1c9f3e7b02d"
down_revision = None
branch_labels = None
depends_on = None


def upgrade(op=None):
    tables = get_all_tables(op)

    if "mayfly_challenges" not in tables:
        op.create_table(
            "mayfly_challenges",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("image", sa.String(length=255), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("ttl_seconds", sa.Integer(), nullable=False),
            sa.Column("vcpus", sa.Integer(), nullable=False),
            sa.Column("start_command", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["id"], ["challenges.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    if "mayfly_instances" not in tables:
        op.create_table(
            "mayfly_instances",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("challenge_id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("url", sa.String(length=512), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("flag_hash", sa.String(length=64), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id"),
        )
        op.create_index(
            "ix_mayfly_instances_owner_challenge",
            "mayfly_instances",
            ["owner_id", "challenge_id"],
        )

    if "mayfly_flag_share_events" not in tables:
        op.create_table(
            "mayfly_flag_share_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("challenge_id", sa.Integer(), nullable=False),
            sa.Column("submitting_owner_id", sa.Integer(), nullable=False),
            sa.Column("source_owner_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade(op=None):
    tables = get_all_tables(op)
    if "mayfly_flag_share_events" in tables:
        op.drop_table("mayfly_flag_share_events")
    if "mayfly_instances" in tables:
        op.drop_index("ix_mayfly_instances_owner_challenge", table_name="mayfly_instances")
        op.drop_table("mayfly_instances")
    if "mayfly_challenges" in tables:
        op.drop_table("mayfly_challenges")
