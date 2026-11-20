from __future__ import annotations

from sqlalchemy import inspect

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import alembic_config, upgrade_database


def test_phase_seven_database_upgrades_to_phase_eight(settings: object) -> None:
    from alembic import command

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    config = alembic_config(settings)  # type: ignore[arg-type]
    command.upgrade(config, "20260911_0005")
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        tables = set(inspect(database.engine).get_table_names())
        assert {
            "monitor_items",
            "release_versions",
            "events",
            "jobs",
            "worker_heartbeats",
            "proxy_profiles",
            "tracker_sessions",
            "plugin_states",
            "torrent_clients",
            "delivery_jobs",
            "notification_channels",
            "notification_jobs",
        } <= tables
        indexes = {item["name"] for item in inspect(database.engine).get_indexes("delivery_jobs")}
        assert "ix_delivery_jobs_claimable" in indexes
        notification_indexes = {
            item["name"] for item in inspect(database.engine).get_indexes("notification_jobs")
        }
        assert "ix_notification_jobs_claimable" in notification_indexes
    finally:
        database.dispose()
