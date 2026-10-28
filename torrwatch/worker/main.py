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

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from torrwatch.core.config import Settings, get_settings
from torrwatch.core.logging import configure_debug_logging
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import DeliveryJob, Job, SystemSetting
from torrwatch.domain.enums import JobType
from torrwatch.notifications.service import NotificationRepository, NotificationService
from torrwatch.notifications.types import NotificationError, NotificationErrorCode
from torrwatch.services.admin_jobs import AdminJobService
from torrwatch.services.delivery import DeliveryRepository, DeliveryService
from torrwatch.services.flaresolverr import (
    FlareSolverrClient,
    FlareSolverrError,
    refresh_kinozal,
    refresh_nnmclub,
    refresh_rutracker,
)
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
        recommendation = f" Рекомендация: {error.recommendation}" if error.recommendation else ""
        logger.error(
            "Проверка задачи %s завершилась ошибкой: %s.%s",
            job.id,
            error.message,
            recommendation,
        )
        if error.technical_message:
            logger.debug("Техническая ошибка задачи %s: %s", job.id, error.technical_message)
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
                    recommendation=error.recommendation,
                    technical_message=error.technical_message,
                )
            else:
                jobs.fail_permanent(
                    job.id,
                    worker_id,
                    error.message,
                    now_utc(),
                    monitor_status=error.monitor_status,
                    event_code=error.event_code,
                    recommendation=error.recommendation,
                    technical_message=error.technical_message,
                )
        return False
    except Exception as error:
        logger.error(
            "Непредвиденная ошибка фоновой задачи %s: %s. Проверьте журнал событий и настройки подключения.",
            job.id,
            error,
        )
        logger.debug("Полная трассировка фоновой задачи %s", job.id, exc_info=True)
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
        logger.error(
            "Ошибка доставки задачи %s: %s. Проверьте торрент-клиент, путь сохранения и его доступность.",
            job.id,
            error,
        )
        logger.debug("Полная трассировка доставки задачи %s", job.id, exc_info=True)
        if owns_job:
            deliveries.fail(job.id, worker_id, str(error), retryable=True)
            NotificationRepository(deliveries.database).enqueue_for_delivery_job(
                job.id, "DELIVERY_FAILED", str(error)
            )
        return False
    if owns_job:
        with deliveries.database.session() as session:
            latest = session.get(DeliveryJob, job.id)
            status = str(latest.status) if latest else ""
            error = latest.last_error if latest else None
        if status == "SUCCESS":
            NotificationRepository(deliveries.database).enqueue_for_delivery_job(
                job.id, "DELIVERY_SUCCESS", "Торрент успешно добавлен в торрент-клиент."
            )
        elif status == "FAILED_PERMANENT":
            NotificationRepository(deliveries.database).enqueue_for_delivery_job(
                job.id, "DELIVERY_FAILED", error or "Доставка завершилась постоянной ошибкой."
            )
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


async def refresh_tracker_sessions(
    database: Database, secrets: SecretBox, automation: dict
) -> None:
    """Refresh tracker sessions independently of the job processing loop."""
    solver = FlareSolverrClient(automation["url"])
    for label, refresh, username, password in (
        ("NNM-Club", refresh_nnmclub, automation["username"], automation["password"]),
        (
            "Kinozal",
            refresh_kinozal,
            automation["kinozal_username"],
            automation["kinozal_password"],
        ),
        (
            "RuTracker",
            refresh_rutracker,
            automation["rutracker_username"],
            automation["rutracker_password"],
        ),
    ):
        if username and password:
            try:
                await refresh(database, secrets, solver, username, password)
            except FlareSolverrError as error:
                logger.warning("Automatic %s session refresh failed: %s", label, error)
    logger.info("Automatic tracker session refresh finished.")


async def run_worker(settings: Settings) -> None:
    """Run the Phase 0 heartbeat loop and shut down cleanly on SIGTERM."""
    ensure_runtime_files(settings)
    if settings.worker_lease_renewal_seconds >= settings.worker_job_lease_seconds:
        raise ValueError("Lease renewal interval must be shorter than the lease lifetime.")
    database = Database(settings.resolved_database_url)
    try:
        with database.session() as session:
            debug = (
                session.scalar(select(SystemSetting.value).where(SystemSetting.key == "debug_mode"))
                == "true"
            )
    except OperationalError:
        # Web startup owns migrations; a worker may briefly start before schema creation.
        debug = False
    logging.getLogger().setLevel(
        logging.DEBUG if debug else getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    configure_debug_logging(
        settings.data_dir,
        "worker",
        logging.DEBUG if debug else getattr(logging, settings.log_level.upper(), logging.INFO),
        lambda: _setting_enabled(database, "debug_redact_secrets"),
    )
    stop_event = asyncio.Event()
    identity = worker_identity()
    secrets = SecretBox(settings.resolved_master_key_file)
    transport = HttpTransport.from_settings(
        settings,
        proxy_service=ProxyService(database, secrets),
        sessions=SessionStore(database, secrets),
    )
    checks = MonitorCheckService(database, settings, load_plugin_registry(settings), transport)
    last_session_refresh = 0.0
    last_credentials_revision = ""
    refresh_task: asyncio.Task[None] | None = None

    async def monitor_handler(job: Job) -> None:
        if job.job_type == JobType.MONITOR_CHECK:
            await checks.handle(job, identity)
        else:
            await AdminJobService(database, secrets).handle(job, identity)

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, stop_event.set)
    try:
        while not stop_event.is_set():
            now_monotonic = asyncio.get_running_loop().time()
            automation = _flaresolverr_config(database, settings, secrets)
            credentials_revision = str(automation["credentials_revision"])
            if credentials_revision != last_credentials_revision:
                last_session_refresh = 0.0
                last_credentials_revision = credentials_revision
            if (
                automation["enabled"]
                and automation["url"]
                and (refresh_task is None or refresh_task.done())
                and (now_monotonic - last_session_refresh >= automation["refresh_seconds"])
            ):
                if refresh_task is not None:
                    try:
                        refresh_task.result()
                    except Exception:
                        logger.exception("Automatic tracker session refresh failed unexpectedly")
                last_session_refresh = now_monotonic
                refresh_task = asyncio.create_task(
                    refresh_tracker_sessions(database, secrets, automation)
                )
            work_done = await worker_cycle(
                database,
                identity,
                handler=monitor_handler,
                lease_seconds=settings.worker_job_lease_seconds,
                renewal_seconds=settings.worker_lease_renewal_seconds,
                stop_event=stop_event,
            )
            if not stop_event.is_set():
                work_done += await delivery_cycle(
                    database,
                    secrets,
                    identity,
                    lease_seconds=settings.worker_job_lease_seconds,
                    renewal_seconds=settings.worker_lease_renewal_seconds,
                    stop_event=stop_event,
                )
            if not stop_event.is_set():
                work_done += await notification_cycle(
                    database,
                    secrets,
                    identity,
                    lease_seconds=settings.worker_job_lease_seconds,
                    renewal_seconds=settings.worker_lease_renewal_seconds,
                    stop_event=stop_event,
                )
            if work_done:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                continue
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Automatic tracker session refresh failed unexpectedly")
        database.dispose()
        logger.info("Worker stopped")


def run() -> None:
    asyncio.run(run_worker(get_settings()))


def _setting_enabled(database: Database, key: str) -> bool:
    try:
        with database.session() as session:
            return (
                session.scalar(select(SystemSetting.value).where(SystemSetting.key == key))
                != "false"
            )
    except Exception:
        return True


def _flaresolverr_config(
    database: Database, settings: Settings, secrets: SecretBox
) -> dict[str, object]:
    """Read UI overrides, falling back to bootstrap environment settings."""
    try:
        with database.session() as session:
            rows = session.scalars(select(SystemSetting)).all()
            relevant_keys = {
                "flaresolverr_url",
                "flaresolverr_enabled",
                "flaresolverr_refresh_seconds",
                "nnmclub_username",
                "nnmclub_password_encrypted",
                "tracker_kinozal_username",
                "tracker_kinozal_password_encrypted",
                "tracker_rutracker_username",
                "tracker_rutracker_password_encrypted",
            }
            relevant = [row for row in rows if row.key in relevant_keys]
            values = {row.key: row.value for row in relevant}
            credentials_revision = "|".join(
                f"{row.key}:{row.updated_at.isoformat()}"
                for row in sorted(relevant, key=lambda item: item.key)
                if "username" in row.key or "password" in row.key
            )
    except Exception:
        values = {}
        credentials_revision = ""
    password = settings.nnmclub_password.get_secret_value() if settings.nnmclub_password else ""
    encrypted = values.get("nnmclub_password_encrypted")
    if encrypted:
        try:
            password = secrets.decrypt(encrypted)
        except Exception:
            logger.warning("Stored NNM-Club password cannot be decrypted; using environment value.")
    try:
        refresh_seconds = max(
            300,
            int(values.get("flaresolverr_refresh_seconds", settings.flaresolverr_refresh_seconds)),
        )
    except (TypeError, ValueError):
        refresh_seconds = settings.flaresolverr_refresh_seconds
    return {
        "url": values.get("flaresolverr_url") or settings.flaresolverr_url,
        "enabled": values.get("flaresolverr_enabled", "true") != "false",
        "refresh_seconds": refresh_seconds,
        "username": values.get("nnmclub_username") or settings.nnmclub_username,
        "password": password,
        "kinozal_username": values.get("tracker_kinozal_username") or "",
        "kinozal_password": _decrypt_optional(
            secrets, values.get("tracker_kinozal_password_encrypted")
        ),
        "rutracker_username": values.get("tracker_rutracker_username") or "",
        "rutracker_password": _decrypt_optional(
            secrets, values.get("tracker_rutracker_password_encrypted")
        ),
        "credentials_revision": credentials_revision,
    }


def _decrypt_optional(secrets: SecretBox, encrypted: str | None) -> str:
    if not encrypted:
        return ""
    try:
        return secrets.decrypt(encrypted)
    except Exception:
        logger.warning("Stored tracker password cannot be decrypted.")
        return ""
