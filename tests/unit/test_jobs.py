from __future__ import annotations

from datetime import timedelta

from torrwatch.services.jobs import JobRepository, MonitorRepository, Scheduler, now_utc


def make_monitor(
    database: object,
    *,
    due_offset: int = 0,
    enabled: bool = True,
    paused: bool = False,
    base_time: object | None = None,
):
    now = base_time or now_utc()
    return MonitorRepository(database).create(  # type: ignore[arg-type]
        name="Example",
        url="https://example.invalid/topic/1",
        plugin_id="fake",
        next_check_at=now + timedelta(seconds=due_offset),
        enabled=enabled,
        paused=paused,
    )


def test_future_disabled_and_paused_monitors_are_not_scheduled(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        now = now_utc()
        future = make_monitor(database, due_offset=60)
        make_monitor(database, enabled=False)
        make_monitor(database, paused=True)
        scheduler = Scheduler(MonitorRepository(database), JobRepository(database))
        assert scheduler.schedule_due(now) == 0
        assert scheduler.schedule_due(now + timedelta(seconds=61)) == 1
        assert future.id > 0
    finally:
        database.dispose()


def test_retry_permanent_success_and_abandoned_lifecycle(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database
    from torrwatch.domain.enums import JobStatus

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        now = now_utc()
        monitor = make_monitor(database, base_time=now)
        jobs = JobRepository(database)
        Scheduler(MonitorRepository(database), jobs).schedule_due(now)
        job = jobs.claim_next("worker-a", now, lease_seconds=1)
        assert job is not None and jobs.fail_retryable(job.id, "worker-a", "temporary", now)
        with database.session() as session:
            retry = session.get(type(job), job.id)
            assert retry is not None and retry.status == JobStatus.FAILED_RETRYABLE
            assert retry.next_attempt_at == now + timedelta(minutes=1)
        assert jobs.claim_next("worker-a", now + timedelta(minutes=1)) is not None
        claimed = jobs.claim_next("worker-b", now + timedelta(minutes=1, seconds=1))
        assert claimed is None
        assert jobs.recover_abandoned(now + timedelta(minutes=4)) == 1
        recovered = jobs.claim_next("worker-c", now + timedelta(minutes=4))
        assert recovered is not None and jobs.complete_success(
            recovered.id, "worker-c", now + timedelta(minutes=4)
        )
        with database.session() as session:
            assert session.get(type(recovered), recovered.id).status == JobStatus.SUCCESS  # type: ignore[union-attr]
            assert session.get(type(monitor), monitor.id).next_check_at == now + timedelta(
                minutes=34
            )  # type: ignore[union-attr]
    finally:
        database.dispose()
