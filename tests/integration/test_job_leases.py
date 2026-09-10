from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from torrwatch.domain.enums import JobStatus
from torrwatch.services.jobs import JobRepository, MonitorRepository, Scheduler, now_utc
from torrwatch.worker.main import execute_claimed_job, worker_cycle


def prepare_due_job(database: object):
    now = now_utc()
    monitor = MonitorRepository(database).create(  # type: ignore[arg-type]
        name="Lease monitor",
        url="https://example.invalid/lease",
        plugin_id="fake",
        next_check_at=now,
    )
    jobs = JobRepository(database)  # type: ignore[arg-type]
    assert Scheduler(MonitorRepository(database), jobs).schedule_due(now) == 1  # type: ignore[arg-type]
    return jobs, monitor, now


def test_active_renewal_prevents_recovery_or_reclaim(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        jobs, _, now = prepare_due_job(database)
        job = jobs.claim_next("worker-a", now, lease_seconds=1)
        assert job is not None

        async def scenario() -> None:
            started = asyncio.Event()

            async def long_handler(_: object) -> None:
                started.set()
                await asyncio.sleep(1.2)

            execution = asyncio.create_task(
                execute_claimed_job(
                    jobs,
                    job,
                    "worker-a",
                    long_handler,  # type: ignore[arg-type]
                    lease_seconds=1,
                    renewal_seconds=0.1,
                )
            )
            await started.wait()
            await asyncio.sleep(1.05)
            assert jobs.recover_abandoned(now_utc()) == 0
            assert jobs.claim_next("worker-b", now_utc()) is None
            assert await execution

        asyncio.run(scenario())
    finally:
        database.dispose()


def test_expired_lease_is_recovered_after_renewal_stops(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        jobs, _, now = prepare_due_job(database)
        job = jobs.claim_next("worker-a", now, lease_seconds=1)
        assert job is not None
        assert jobs.renew_lease(job.id, "worker-a", now + timedelta(seconds=1), 2)
        assert jobs.recover_abandoned(now + timedelta(seconds=2)) == 0
        assert jobs.recover_abandoned(now + timedelta(seconds=4)) == 1
    finally:
        database.dispose()


def test_only_current_owner_can_renew_or_complete(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        jobs, _, now = prepare_due_job(database)
        job = jobs.claim_next("worker-a", now)
        assert job is not None
        assert not jobs.renew_lease(job.id, "worker-b", now, 180)
        assert jobs.recover_abandoned(now + timedelta(minutes=3)) == 1
        assert not jobs.complete_success(job.id, "worker-a", now + timedelta(minutes=3))
        assert not jobs.fail_retryable(job.id, "worker-a", "late", now + timedelta(minutes=3))
    finally:
        database.dispose()


def test_two_workers_still_claim_exactly_one_owner(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        jobs, _, now = prepare_due_job(database)
        barrier = Barrier(2)

        def claim(worker_id: str) -> int | None:
            barrier.wait()
            job = JobRepository(database).claim_next(worker_id, now)  # type: ignore[arg-type]
            return None if job is None else job.id

        with ThreadPoolExecutor(max_workers=2) as executor:
            winners = list(executor.map(claim, ["worker-a", "worker-b"]))
        assert sum(winner is not None for winner in winners) == 1
    finally:
        database.dispose()


def test_sqlite_claim_contention_does_not_crash_worker_cycle(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        _, _, now = prepare_due_job(database)
        original_execute = Session.execute
        update_calls = 0

        def execute_with_busy(self: Session, statement: object, *args: object, **kwargs: object):
            nonlocal update_calls
            if statement.__class__.__name__ == "Update":
                update_calls += 1
                if (
                    update_calls == 2
                ):  # recovery UPDATE is first; conditional claim UPDATE is second.
                    raise OperationalError(
                        "claim contention", {}, sqlite3.OperationalError("database is locked")
                    )
            return original_execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "execute", execute_with_busy)
        assert asyncio.run(worker_cycle(database, "worker-a", now)) == 0
    finally:
        database.dispose()


def test_handler_execution_keeps_no_database_session_open(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        jobs, _, now = prepare_due_job(database)
        job = jobs.claim_next("worker-a", now)
        assert job is not None
        original_session = database.session
        active_sessions = 0

        @contextmanager
        def counted_session():
            nonlocal active_sessions
            active_sessions += 1
            try:
                with original_session() as session:
                    yield session
            finally:
                active_sessions -= 1

        database.session = counted_session  # type: ignore[method-assign]

        async def scenario() -> None:
            started = asyncio.Event()

            async def handler(_: object) -> None:
                started.set()
                await asyncio.sleep(0.1)

            execution = asyncio.create_task(
                execute_claimed_job(
                    jobs,
                    job,
                    "worker-a",
                    handler,  # type: ignore[arg-type]
                    lease_seconds=2,
                    renewal_seconds=1,
                )
            )
            await started.wait()
            assert active_sessions == 0
            assert await execution

        asyncio.run(scenario())
    finally:
        database.dispose()


def test_shutdown_request_does_not_claim_another_job(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database
    from torrwatch.db.models import Job

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        _, _, now = prepare_due_job(database)
        stop_event = asyncio.Event()
        stop_event.set()
        assert asyncio.run(worker_cycle(database, "worker-a", now, stop_event=stop_event)) == 0
        with database.session() as session:
            assert session.query(Job.status).scalar() == JobStatus.PENDING
    finally:
        database.dispose()
