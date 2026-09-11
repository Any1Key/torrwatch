"""Short-transaction repositories for scheduling, claiming and job lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError

from torrwatch.db.database import Database
from torrwatch.db.models import Event, Job, MonitorItem, WorkerHeartbeat
from torrwatch.domain.enums import CLAIMABLE_JOB_STATUSES, JobStatus, JobType, MonitorStatus

MIN_CHECK_INTERVAL_SECONDS = 300
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 10800)


@dataclass(frozen=True)
class JobExecutionFailure(Exception):
    """Sanitized monitor outcome mapped deliberately to durable job state."""

    message: str
    retryable: bool
    monitor_status: MonitorStatus = MonitorStatus.ERROR
    retry_delay_seconds: int | None = None
    event_code: str = "CHECK_FAILED"


def now_utc() -> datetime:
    return datetime.now(UTC)


def active_check_key(monitor_id: int) -> str:
    return f"monitor-check:{monitor_id}"


def is_sqlite_busy(error: OperationalError) -> bool:
    """Recognize only the expected SQLite lock contention handled by job claims."""
    if not isinstance(error.orig, sqlite3.OperationalError):
        return False
    message = str(error.orig).lower()
    return "database is locked" in message or "database is busy" in message


class MonitorRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        name: str,
        url: str,
        plugin_id: str | None,
        next_check_at: datetime,
        interval_seconds: int = 1800,
        enabled: bool = True,
        paused: bool = False,
    ) -> MonitorItem:
        if interval_seconds < MIN_CHECK_INTERVAL_SECONDS:
            raise ValueError(
                f"Monitor interval must be at least {MIN_CHECK_INTERVAL_SECONDS} seconds."
            )
        with self.database.session() as session:
            monitor = MonitorItem(
                name=name,
                original_url=url,
                canonical_url=url,
                plugin_id=plugin_id,
                next_check_at=next_check_at,
                check_interval_seconds=interval_seconds,
                enabled=enabled,
                paused=paused,
                current_status=MonitorStatus.PAUSED if paused else MonitorStatus.HEALTHY,
            )
            session.add(monitor)
            session.flush()
            return monitor

    def due(self, now: datetime) -> list[MonitorItem]:
        with self.database.session() as session:
            query: Select[tuple[MonitorItem]] = (
                select(MonitorItem)
                .where(
                    MonitorItem.enabled.is_(True),
                    MonitorItem.paused.is_(False),
                    MonitorItem.next_check_at.is_not(None),
                    MonitorItem.next_check_at <= now,
                )
                .order_by(MonitorItem.next_check_at, MonitorItem.id)
            )
            return list(session.scalars(query))


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue_monitor_check(self, monitor_id: int, now: datetime) -> bool:
        statement = (
            insert(Job)
            .values(
                job_type=JobType.MONITOR_CHECK,
                monitor_id=monitor_id,
                status=JobStatus.PENDING,
                active_key=active_check_key(monitor_id),
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Job.active_key])
        )
        with self.database.session() as session:
            result = session.execute(statement)
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                return False
            session.add(
                Event(
                    monitor_id=monitor_id,
                    level="INFO",
                    event_code="CHECK_SCHEDULED",
                    message="Monitor check scheduled.",
                    details_json="{}",
                    created_at=now,
                )
            )
            return True

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: int = 120) -> Job | None:
        try:
            with self.database.session() as session:
                candidate = session.scalar(
                    select(Job.id)
                    .where(Job.status.in_(CLAIMABLE_JOB_STATUSES), Job.next_attempt_at <= now)
                    .order_by(Job.next_attempt_at, Job.id)
                    .limit(1)
                )
                if candidate is None:
                    return None
                result = session.execute(
                    update(Job)
                    .where(
                        Job.id == candidate,
                        Job.status.in_(CLAIMABLE_JOB_STATUSES),
                        Job.next_attempt_at <= now,
                    )
                    .values(
                        status=JobStatus.RUNNING,
                        worker_id=worker_id,
                        claimed_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        started_at=func.coalesce(Job.started_at, now),
                        attempts=Job.attempts + 1,
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    return None
                return session.get(Job, candidate)
        except OperationalError as error:
            if is_sqlite_busy(error):
                return None
            raise

    def renew_lease(self, job_id: int, worker_id: str, now: datetime, lease_seconds: int) -> bool:
        """Atomically extend a lease only while this worker still owns the running job."""
        with self.database.session() as session:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.worker_id == worker_id,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def complete_success(self, job_id: int, worker_id: str, now: datetime) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING or job.worker_id != worker_id:
                return False
            job.status, job.completed_at, job.lease_expires_at, job.active_key = (
                JobStatus.SUCCESS,
                now,
                None,
                None,
            )
            if job.monitor_id is not None:
                monitor = session.get(MonitorItem, job.monitor_id)
                if monitor is not None:
                    monitor.last_check_at = now
                    monitor.last_success_at = now
                    monitor.next_check_at = now + timedelta(seconds=monitor.check_interval_seconds)
                    monitor.consecutive_failures = 0
                    monitor.current_status = MonitorStatus.HEALTHY
                session.add(
                    Event(
                        monitor_id=job.monitor_id,
                        level="INFO",
                        event_code="CHECK_SUCCESS",
                        message="Monitor check completed.",
                        details_json="{}",
                        created_at=now,
                    )
                )
            return True

    def fail_retryable(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        now: datetime,
        *,
        monitor_status: MonitorStatus = MonitorStatus.ERROR,
        retry_delay_seconds: int | None = None,
        event_code: str = "CHECK_RETRY_SCHEDULED",
    ) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING or job.worker_id != worker_id:
                return False
            delay = (
                retry_delay_seconds
                or RETRY_DELAYS_SECONDS[min(job.attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            )
            job.status = JobStatus.FAILED_RETRYABLE
            job.next_attempt_at = now + timedelta(seconds=delay)
            job.lease_expires_at = None
            job.last_error = error
            if job.monitor_id is not None:
                monitor = session.get(MonitorItem, job.monitor_id)
                if monitor is not None:
                    monitor.last_check_at = now
                    monitor.consecutive_failures += 1
                    monitor.current_status = monitor_status
                session.add(
                    Event(
                        monitor_id=job.monitor_id,
                        level="WARNING",
                        event_code=event_code,
                        message="Monitor check will be retried.",
                        details_json="{}",
                        created_at=now,
                    )
                )
            return True

    def fail_permanent(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        now: datetime,
        *,
        monitor_status: MonitorStatus = MonitorStatus.ERROR,
        event_code: str = "CHECK_FAILED_PERMANENT",
    ) -> bool:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING or job.worker_id != worker_id:
                return False
            job.status, job.completed_at, job.lease_expires_at, job.active_key, job.last_error = (
                JobStatus.FAILED_PERMANENT,
                now,
                None,
                None,
                error,
            )
            if job.monitor_id is not None:
                monitor = session.get(MonitorItem, job.monitor_id)
                if monitor is not None:
                    monitor.last_check_at = now
                    monitor.next_check_at = None
                    monitor.current_status = monitor_status
                    monitor.consecutive_failures += 1
                session.add(
                    Event(
                        monitor_id=job.monitor_id,
                        level="ERROR",
                        event_code=event_code,
                        message="Monitor check failed permanently.",
                        details_json="{}",
                        created_at=now,
                    )
                )
            return True

    def recover_abandoned(self, now: datetime) -> int:
        with self.database.session() as session:
            result = session.execute(
                update(Job)
                .where(
                    Job.status == JobStatus.RUNNING,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at < now,
                )
                .values(
                    status=JobStatus.FAILED_RETRYABLE,
                    worker_id=None,
                    lease_expires_at=None,
                    next_attempt_at=now,
                    last_error="Worker lease expired; job recovered.",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def heartbeat(self, worker_id: str, now: datetime) -> None:
        statement = (
            insert(WorkerHeartbeat)
            .values(worker_id=worker_id, started_at=now, heartbeat_at=now)
            .on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_id], set_={"heartbeat_at": now}
            )
        )
        with self.database.session() as session:
            session.execute(statement)


class Scheduler:
    def __init__(self, monitors: MonitorRepository, jobs: JobRepository) -> None:
        self.monitors, self.jobs = monitors, jobs

    def schedule_due(self, now: datetime) -> int:
        return sum(
            self.jobs.enqueue_monitor_check(monitor.id, now) for monitor in self.monitors.due(now)
        )
