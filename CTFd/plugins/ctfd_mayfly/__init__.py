"""
Entry point CTFd's plugin loader calls: CTFd.plugins.init_plugins() imports this package and
calls load(app) for every directory under CTFd/plugins/ (see that function for exactly how).
"""
import os

import sqlalchemy as sa

from CTFd.models import db
from CTFd.plugins import register_admin_plugin_menu_bar, register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.plugins.migrations import upgrade

from .api import admin_bp, internal_bp, player_bp
from .challenge import MayflyChallenge


def _ensure_run_token_column():
    """CTFd.plugins.migrations.upgrade() calls db.create_all() directly for SQLite and never
    even looks at migrations/ (see that directory's b7d4a1f9c3e6 revision) — and create_all()
    only creates tables that don't exist yet, it never alters one that already does. That's
    fine for a fresh install, but this project's live Cloud Run deployment already has a
    mayfly_instances table from before run_token existed, on SQLite, so nothing above would
    ever add the column to it. Safe to run on every boot regardless of backend: checks first,
    so it's a no-op everywhere the column (or the table) already exists correctly."""
    inspector = sa.inspect(db.engine)
    if "mayfly_instances" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("mayfly_instances")}
    if "run_token" not in columns:
        with db.engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE mayfly_instances ADD COLUMN run_token VARCHAR(128)"))


def load(app):
    # SKIP_PLUGIN_MIGRATIONS: both upgrade() and _ensure_run_token_column() open their own
    # DB connection and run at least one round trip EVERY app boot, unconditionally, even
    # when there's nothing to do -- fine on Cloud Run (one long-lived pinned instance, boots
    # rarely) but a real, avoidable cost on a platform where a fresh instance can boot on any
    # request (see the orchestrator's CLAUDE.md boundary: this repo doesn't dictate that
    # platform's tradeoffs). An explicit, named opt-out -- never silently skipped -- accepted
    # for the Vercel demo deployment specifically, whose schema was already fully migrated
    # via the real Alembic path (Postgres, not SQLite) before this flag was ever set. A future
    # migration added to either CTFd core or this plugin needs `flask db upgrade` /
    # `upgrade(plugin_name="ctfd_mayfly")` run manually once while this is set, or this flag
    # temporarily unset, before it will actually apply.
    if os.environ.get("SKIP_PLUGIN_MIGRATIONS", "false").lower() != "true":
        # For SQLite this just calls db.create_all(); for anything else (MySQL in production —
        # see the orchestrator's CLAUDE.md: "CTFd, its MySQL... stay on GCP") it runs the real
        # Alembic revision under migrations/, which is why that file exists rather than relying
        # on db.create_all() alone.
        upgrade(plugin_name="ctfd_mayfly")
        _ensure_run_token_column()

    CHALLENGE_CLASSES["mayfly"] = MayflyChallenge
    register_plugin_assets_directory(app, base_path="/plugins/ctfd_mayfly/assets/")

    app.register_blueprint(player_bp)
    app.register_blueprint(internal_bp)
    app.register_blueprint(admin_bp)

    register_admin_plugin_menu_bar(title="Mayfly", route="/plugins/ctfd_mayfly/admin/instances")
