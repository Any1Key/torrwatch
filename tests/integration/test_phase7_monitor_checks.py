from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import DeliveryJob, MonitorItem, ReleaseVersion
from torrwatch.services.jobs import JobRepository, MonitorRepository, now_utc
from torrwatch.services.monitor_checks import MonitorCheckService
from torrwatch.trackers.loader import load_plugin_registry
from torrwatch.transport.http import TransportResponse
from torrwatch.worker.main import execute_claimed_job

FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass
class FixtureTransport:
    page_url: str
    download_url: str
    page: bytes
    torrent: bytes
    calls: list[str]

    async def request(self, method: str, url: str, **kwargs: object) -> TransportResponse:
        del method, kwargs
        self.calls.append(url)
        if url == self.page_url:
            return TransportResponse(200, {}, self.page, url, 1, 1)
        if url == self.download_url:
            return TransportResponse(200, {}, self.torrent, url, 1, 1)
        raise AssertionError(f"Unexpected fixture request: {url}")


def _torrent() -> bytes:
    return (FIXTURES / "torrents" / "valid-v1.torrent").read_bytes().removesuffix(b"\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_id", "url", "download_url", "fixture"),
    [
        (
            "nnmclub",
            "https://nnmclub.to/forum/viewtopic.php?t=23456",
            "https://nnmclub.to/forum/dl.php?t=23456",
            "nnmclub/topic.html",
        ),
        (
            "kinozal",
            "https://kinozal.tv/details.php?id=34567",
            "https://dl.kinozal.tv/download.php?id=34567",
            "kinozal/release.html",
        ),
    ],
)
async def test_phase7_plugins_reuse_baseline_and_authoritative_change_pipeline(
    settings: object, plugin_id: str, url: str, download_url: str, fixture: str
) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        monitor = MonitorRepository(database).create(
            name=plugin_id, url=url, plugin_id=plugin_id, next_check_at=now_utc()
        )
        transport = FixtureTransport(
            url, download_url, (FIXTURES / fixture).read_bytes(), _torrent(), []
        )
        checks = MonitorCheckService(
            database,
            settings,  # type: ignore[arg-type]
            load_plugin_registry(settings),  # type: ignore[arg-type]
            transport,  # type: ignore[arg-type]
        )
        jobs = JobRepository(database)
        assert jobs.enqueue_monitor_check(monitor.id, now_utc())
        job = jobs.claim_next("worker-a", now_utc(), 60)
        assert job is not None
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-a",
            lambda claimed: checks.handle(claimed, "worker-a"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        with database.session() as session:
            saved = session.get(MonitorItem, monitor.id)
            assert saved is not None and saved.current_release_id is not None
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 1
            assert (
                session.query(DeliveryJob).count() == 0
            )  # Initial baseline is never delivered by default.

        # The same authoritative torrent under changed page metadata does not
        # create history or delivery; the generic service makes this decision.
        transport.page = transport.page.replace(b"fixture-revision", b"changed-revision")
        transport.calls.clear()
        assert jobs.enqueue_monitor_check(monitor.id, now_utc())
        retry = jobs.claim_next("worker-b", now_utc(), 60)
        assert retry is not None
        assert await execute_claimed_job(
            jobs,
            retry,
            "worker-b",
            lambda claimed: checks.handle(claimed, "worker-b"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        with database.session() as session:
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 1
            assert session.query(DeliveryJob).count() == 0
    finally:
        database.dispose()
