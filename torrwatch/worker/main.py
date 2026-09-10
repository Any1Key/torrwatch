"""Independent worker process for scheduled background work."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid
from contextlib import suppress
from datetime import datetime

from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.services.jobs import JobRepository, MonitorRepository, Scheduler, now_utc

logger = logging.getLogger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4()}"


def worker_cycle(database: Database, worker_id: str, now: datetime | None = None) -> int:
    """Run one no-network scheduling and fake-handler cycle for Phase 1."""
    current = now or now_utc()
    jobs = JobRepository(database)
    jobs.heartbeat(worker_id, current)
    jobs.recover_abandoned(current)
    Scheduler(MonitorRepository(database), jobs).schedule_due(current)
    job = jobs.claim_next(worker_id, current)
    if job is None:
        return 0
    # Tracker execution deliberately does not exist until later phases.
    jobs.complete_success(job.id, worker_id, current)
    return 1


async def run_worker(settings: Settings) -> None:
    """Run the Phase 0 heartbeat loop and shut down cleanly on SIGTERM."""
    ensure_runtime_files(settings)
    database = Database(settings.resolved_database_url)
    stop_event = asyncio.Event()
    identity = worker_identity()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        while not stop_event.is_set():
            worker_cycle(database, identity)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_heartbeat_seconds)
            except TimeoutError:
                continue
    finally:
        database.dispose()
        logger.info("Worker stopped")


def run() -> None:
    asyncio.run(run_worker(get_settings()))
