from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import MonitorItem, ReleaseVersion
from torrwatch.services.jobs import JobRepository, MonitorRepository, now_utc
from torrwatch.services.monitor_checks import MonitorCheckService
from torrwatch.trackers.loader import load_plugin_registry
from torrwatch.transport.http import TransportResponse
from torrwatch.worker.main import execute_claimed_job

FIXTURES = Path(__file__).parents[1] / "fixtures"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t=12345"
DOWNLOAD_URL = "https://rutracker.org/forum/dl.php?t=12345"


def _torrent_fixture() -> bytes:
    return (FIXTURES / "torrents" / "valid-v1.torrent").read_bytes().removesuffix(b"\n")


@dataclass
class FixtureTransport:
    page: bytes
    torrent: bytes
    calls: list[tuple[str, str]]

    async def request(self, method: str, url: str, **kwargs: object) -> TransportResponse:
        del kwargs
        self.calls.append((method, url))
        if url == TOPIC_URL:
            return TransportResponse(200, {}, self.page, url, 1, 1)
        if url == DOWNLOAD_URL:
            return TransportResponse(200, {}, self.torrent, url, 1, 1)
        raise AssertionError(f"Unexpected fixture request: {url}")


def _claim(database: Database, monitor_id: int, worker_id: str):
    jobs = JobRepository(database)
    assert jobs.enqueue_monitor_check(monitor_id, now_utc())
    job = jobs.claim_next(worker_id, now_utc(), lease_seconds=60)
    assert job is not None
    return jobs, job


@pytest.mark.asyncio
async def test_rutracker_baseline_and_authoritative_change_detection(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        monitor = MonitorRepository(database).create(
            name="RuTracker fixture",
            url=TOPIC_URL,
            plugin_id="rutracker",
            next_check_at=now_utc(),
        )
        page = (FIXTURES / "rutracker" / "topic.html").read_bytes()
        first_torrent = _torrent_fixture()
        transport = FixtureTransport(page, first_torrent, [])
        service = MonitorCheckService(
            database,
            settings,  # type: ignore[arg-type]
            load_plugin_registry(settings),  # type: ignore[arg-type]
            transport,  # type: ignore[arg-type]
        )

        jobs, job = _claim(database, monitor.id, "worker-a")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-a",
            lambda claimed: service.handle(claimed, "worker-a"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        with database.session() as session:
            saved = session.get(MonitorItem, monitor.id)
            assert saved is not None and saved.current_release_id is not None
            first_release = session.get(ReleaseVersion, saved.current_release_id)
            assert first_release is not None and first_release.file_path is not None
            assert Path(first_release.file_path).is_file()
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 1

        # An unchanged preliminary page state must not fetch a torrent again.
        transport.calls.clear()
        jobs, job = _claim(database, monitor.id, "worker-b")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-b",
            lambda claimed: service.handle(claimed, "worker-b"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        assert transport.calls == [("GET", TOPIC_URL)]

        # Changed HTML with the same authoritative infohash is metadata only.
        transport.page = page.replace(b"revision-42", b"revision-43")
        transport.calls.clear()
        jobs, job = _claim(database, monitor.id, "worker-c")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-c",
            lambda claimed: service.handle(claimed, "worker-c"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        assert transport.calls == [("GET", TOPIC_URL), ("GET", DOWNLOAD_URL)]
        with database.session() as session:
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 1

        # A changed page plus a different validated infohash creates one new release.
        transport.page = transport.page.replace(b"revision-43", b"revision-44")
        transport.torrent = first_torrent.replace(b"test", b"diff", 1)
        jobs, job = _claim(database, monitor.id, "worker-d")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-d",
            lambda claimed: service.handle(claimed, "worker-d"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        with database.session() as session:
            saved = session.get(MonitorItem, monitor.id)
            assert saved is not None and saved.current_release_id is not None
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 2
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_rutracker_forced_verification_can_find_a_changed_torrent(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        monitor = MonitorRepository(database).create(
            name="RuTracker fixture",
            url=TOPIC_URL,
            plugin_id="rutracker",
            next_check_at=now_utc(),
        )
        page = (FIXTURES / "rutracker" / "topic.html").read_bytes()
        original = _torrent_fixture()
        transport = FixtureTransport(page, original, [])
        service = MonitorCheckService(
            database,
            settings,  # type: ignore[arg-type]
            load_plugin_registry(settings),  # type: ignore[arg-type]
            transport,  # type: ignore[arg-type]
        )
        jobs, job = _claim(database, monitor.id, "worker-a")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-a",
            lambda claimed: service.handle(claimed, "worker-a"),
            lease_seconds=60,
            renewal_seconds=1,
        )

        # The default forced interval is 24 hours. Make the stored verification old.
        from torrwatch.trackers.state import PluginStateNamespace

        PluginStateNamespace(database, "rutracker", f"monitor:{monitor.id}").set(
            "last_torrent_verification_at", (now_utc() - timedelta(days=2)).isoformat()
        )
        transport.torrent = original.replace(b"test", b"next", 1)
        transport.calls.clear()
        jobs, job = _claim(database, monitor.id, "worker-b")
        assert await execute_claimed_job(
            jobs,
            job,
            "worker-b",
            lambda claimed: service.handle(claimed, "worker-b"),
            lease_seconds=60,
            renewal_seconds=1,
        )
        assert transport.calls == [("GET", TOPIC_URL), ("GET", DOWNLOAD_URL)]
        with database.session() as session:
            assert session.query(ReleaseVersion).filter_by(monitor_id=monitor.id).count() == 2
    finally:
        database.dispose()
