from __future__ import annotations

from sqlalchemy import inspect

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import alembic_config, upgrade_database


def test_phase_zero_database_upgrades_to_phase_one(settings: object) -> None:
    from alembic import command

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    config = alembic_config(settings)  # type: ignore[arg-type]
    command.upgrade(config, "20260910_0001")
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
        } <= tables
    finally:
        database.dispose()
