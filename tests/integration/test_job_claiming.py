from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from torrwatch.services.jobs import JobRepository, MonitorRepository, Scheduler, now_utc


def test_two_workers_atomically_claim_only_one_job(settings: object) -> None:
    from torrwatch.core.runtime import ensure_runtime_files
    from torrwatch.db.database import Database
    from torrwatch.db.migrations import upgrade_database

    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        now = now_utc()
        monitor = MonitorRepository(database).create(
            name="M", url="https://example.invalid", plugin_id="fake", next_check_at=now
        )
        jobs = JobRepository(database)
        assert Scheduler(MonitorRepository(database), jobs).schedule_due(now) == 1
        barrier = Barrier(2)

        def claim(worker: str) -> int | None:
            barrier.wait()
            job = JobRepository(database).claim_next(worker, now + timedelta(seconds=1))
            return job.id if job else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(claim, ["a", "b"]))
        assert sum(value is not None for value in claimed) == 1
        assert (
            Scheduler(MonitorRepository(database), jobs).schedule_due(now + timedelta(seconds=2))
            == 0
        )
        assert monitor.id > 0
    finally:
        database.dispose()
