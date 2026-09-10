from __future__ import annotations

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import WorkerState
from torrwatch.worker.main import record_heartbeat


def test_worker_heartbeat_is_created_and_updated(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        record_heartbeat(database)
        record_heartbeat(database)
        with database.session() as session:
            assert session.get(WorkerState, 1) is not None
    finally:
        database.dispose()
