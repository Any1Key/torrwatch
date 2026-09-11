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
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import DeliveryJob, Job
from torrwatch.notifications.service import NotificationRepository, NotificationService
from torrwatch.notifications.types import NotificationError, NotificationErrorCode
from torrwatch.services.delivery import DeliveryRepository, DeliveryService
from torrwatch.services.jobs import (
    JobExecutionFailure,
    JobRepository,
    MonitorRepository,
    Scheduler,
    now_utc,
)
from torrwatch.services.monitor_checks import MonitorCheckService
from torrwatch.trackers.loader import load_plugin_registry
from torrwatch.transport.cookies import SessionStore
from torrwatch.transport.http import HttpTransport
from torrwatch.transport.proxy import ProxyService

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
    except JobExecutionFailure as error:
        if owns_job:
            if error.retryable:
                jobs.fail_retryable(
                    job.id,
                    worker_id,
                    error.message,
                    now_utc(),
                    monitor_status=error.monitor_status,
                    retry_delay_seconds=error.retry_delay_seconds,
                    event_code=error.event_code,
                )
            else:
                jobs.fail_permanent(
                    job.id,
                    worker_id,
                    error.message,
                    now_utc(),
                    monitor_status=error.monitor_status,
                    event_code=error.event_code,
                )
        return False
    except Exception as error:
        if owns_job:
            jobs.fail_retryable(job.id, worker_id, str(error), now_utc())
        return False
    return owns_job and jobs.complete_success(job.id, worker_id, now_utc())


async def execute_claimed_delivery(
    deliveries: DeliveryRepository,
    service: DeliveryService,
    job: DeliveryJob,
    worker_id: str,
    *,
    lease_seconds: int,
    renewal_seconds: int,
) -> bool:
    """Execute delivery outside DB transactions while renewing its owned lease.

    ``DeliveryService`` performs the final conditional state transition itself;
    consequently an ownership loss can never be committed by the old worker.
    """
    delivery_task: asyncio.Future[bool] = asyncio.ensure_future(service.deliver(job, worker_id))
    owns_job = True
    while not delivery_task.done():
        await asyncio.wait({delivery_task}, timeout=renewal_seconds)
        if delivery_task.done():
            break
        if not deliveries.renew_lease(job.id, worker_id, lease_seconds):
            owns_job = False
    try:
        completed = await delivery_task
    except Exception as error:
        if owns_job:
            deliveries.fail(job.id, worker_id, str(error), retryable=True)
        return False
    return owns_job and completed


async def delivery_cycle(
    database: Database,
    secrets: SecretBox,
    worker_id: str,
    *,
    lease_seconds: int = 180,
    renewal_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Claim at most one durable delivery, unless shutdown has begun."""
    if renewal_seconds >= lease_seconds:
        raise ValueError("Lease renewal interval must be shorter than the lease lifetime.")
    deliveries = DeliveryRepository(database)
    deliveries.recover_abandoned()
    if stop_event is not None and stop_event.is_set():
        return 0
    job = deliveries.claim_next(worker_id, lease_seconds)
    if job is None:
        return 0
    if stop_event is not None and stop_event.is_set():
        deliveries.fail(job.id, worker_id, "Worker shutdown before execution.", retryable=True)
        return 0
    return int(
        await execute_claimed_delivery(
            deliveries,
            DeliveryService(database, secrets),
            job,
            worker_id,
            lease_seconds=lease_seconds,
            renewal_seconds=renewal_seconds,
        )
    )


async def notification_cycle(
    database: Database,
    secrets: SecretBox,
    worker_id: str,
    *,
    lease_seconds: int = 180,
    renewal_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Process one persisted notification; no new claim during shutdown."""
    repository = NotificationRepository(database)
    repository.recover_abandoned()
    if stop_event is not None and stop_event.is_set():
        return 0
    job = repository.claim_next(worker_id, lease_seconds)
    if job is None:
        return 0
    if stop_event is not None and stop_event.is_set():
        repository.finish(
            job.id,
            worker_id,
            NotificationError(
                NotificationErrorCode.UNAVAILABLE, "Worker shutdown before notification execution."
            ),
        )
        return 0
    return int(
        await execute_claimed_notification(
            repository,
            NotificationService(database, secrets),
            job,
            worker_id,
            lease_seconds=lease_seconds,
            renewal_seconds=renewal_seconds,
        )
    )


async def execute_claimed_notification(
    repository: NotificationRepository,
    service: NotificationService,
    job: object,
    worker_id: str,
    *,
    lease_seconds: int,
    renewal_seconds: int,
) -> bool:
    """Renew ownership while a notification adapter performs outbound I/O."""
    task = asyncio.ensure_future(service.send(job, worker_id))  # type: ignore[arg-type]
    owns = True
    while not task.done():
        await asyncio.wait({task}, timeout=renewal_seconds)
        if not task.done() and not repository.renew_lease(job.id, worker_id, lease_seconds):  # type: ignore[attr-defined]
            owns = False
    return owns and await task


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
    secrets = SecretBox(settings.resolved_master_key_file)
    transport = HttpTransport.from_settings(
        settings,
        proxy_service=ProxyService(database, secrets),
        sessions=SessionStore(database, secrets),
    )
    checks = MonitorCheckService(database, settings, load_plugin_registry(settings), transport)

    async def monitor_handler(job: Job) -> None:
        await checks.handle(job, identity)

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        while not stop_event.is_set():
            await worker_cycle(
                database,
                identity,
                handler=monitor_handler,
                lease_seconds=settings.worker_job_lease_seconds,
                renewal_seconds=settings.worker_lease_renewal_seconds,
                stop_event=stop_event,
            )
            if not stop_event.is_set():
                await delivery_cycle(
                    database,
                    secrets,
                    identity,
                    lease_seconds=settings.worker_job_lease_seconds,
                    renewal_seconds=settings.worker_lease_renewal_seconds,
                    stop_event=stop_event,
                )
            if not stop_event.is_set():
                await notification_cycle(
                    database,
                    secrets,
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
