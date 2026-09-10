"""Independent worker process for scheduled background work."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select

from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import WorkerState

logger = logging.getLogger(__name__)


def record_heartbeat(database: Database) -> None:
    """Persist worker liveness; later phases extend this to job ownership state."""
    now = datetime.now(UTC)
    with database.session() as database_session:
        state = database_session.scalar(select(WorkerState).where(WorkerState.id == 1))
        if state is None:
            database_session.add(WorkerState(id=1, heartbeat_at=now))
        else:
            state.heartbeat_at = now


async def run_worker(settings: Settings) -> None:
    """Run the Phase 0 heartbeat loop and shut down cleanly on SIGTERM."""
    ensure_runtime_files(settings)
    upgrade_database(settings)
    database = Database(settings.resolved_database_url)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        while not stop_event.is_set():
            record_heartbeat(database)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_heartbeat_seconds)
            except TimeoutError:
                continue
    finally:
        database.dispose()
        logger.info("Worker stopped")


def run() -> None:
    asyncio.run(run_worker(get_settings()))
