"""Independent worker process for scheduled background work."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime

from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.models import Job
from torrwatch.services.jobs import JobRepository, MonitorRepository, Scheduler, now_utc

logger = logging.getLogger(__name__)
JobHandler = Callable[[Job], Awaitable[None]]


def worker_identity() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4()}"


async def no_op_handler(_: Job) -> None:
    """Phase 1 placeholder; later phases supply an asynchronous monitor handler."""


async def execute_claimed_job(
    jobs: JobRepository,
    job: Job,
    worker_id: str,
    handler: JobHandler,
    *,
    lease_seconds: int,
    renewal_seconds: int,
) -> bool:
    """Run a handler outside transactions while periodically renewing owned job lease."""
    handler_task: asyncio.Future[None] = asyncio.ensure_future(handler(job))
    owns_job = True
    while not handler_task.done():
        await asyncio.wait({handler_task}, timeout=renewal_seconds)
        if handler_task.done():
            break
        if not jobs.renew_lease(job.id, worker_id, now_utc(), lease_seconds):
            owns_job = False
    try:
        await handler_task
    except Exception as error:
        if owns_job:
            jobs.fail_retryable(job.id, worker_id, str(error), now_utc())
        return False
    return owns_job and jobs.complete_success(job.id, worker_id, now_utc())


async def worker_cycle(
    database: Database,
    worker_id: str,
    now: datetime | None = None,
    *,
    handler: JobHandler = no_op_handler,
    lease_seconds: int = 180,
    renewal_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Run one scheduling cycle without holding transactions across handler execution."""
    if renewal_seconds >= lease_seconds:
        raise ValueError("Lease renewal interval must be shorter than the lease lifetime.")
    current = now or now_utc()
    jobs = JobRepository(database)
    jobs.heartbeat(worker_id, current)
    jobs.recover_abandoned(current)
    Scheduler(MonitorRepository(database), jobs).schedule_due(current)
    if stop_event is not None and stop_event.is_set():
        return 0
    job = jobs.claim_next(worker_id, current, lease_seconds)
    if job is None:
        return 0
    if stop_event is not None and stop_event.is_set():
        jobs.fail_retryable(job.id, worker_id, "Worker shutdown before execution.", now_utc())
        return 0
    return int(
        await execute_claimed_job(
            jobs,
            job,
            worker_id,
            handler,
            lease_seconds=lease_seconds,
            renewal_seconds=renewal_seconds,
        )
    )


async def run_worker(settings: Settings) -> None:
    """Run the Phase 0 heartbeat loop and shut down cleanly on SIGTERM."""
    ensure_runtime_files(settings)
    if settings.worker_lease_renewal_seconds >= settings.worker_job_lease_seconds:
        raise ValueError("Lease renewal interval must be shorter than the lease lifetime.")
    database = Database(settings.resolved_database_url)
    stop_event = asyncio.Event()
    identity = worker_identity()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        while not stop_event.is_set():
            await worker_cycle(
                database,
                identity,
                lease_seconds=settings.worker_job_lease_seconds,
                renewal_seconds=settings.worker_lease_renewal_seconds,
                stop_event=stop_event,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_heartbeat_seconds)
            except TimeoutError:
                continue
    finally:
        database.dispose()
        logger.info("Worker stopped")


def run() -> None:
    asyncio.run(run_worker(get_settings()))
