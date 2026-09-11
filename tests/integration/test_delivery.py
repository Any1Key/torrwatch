from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from torrwatch.clients.types import (
    AddOptions,
    AddResult,
    ClientErrorCode,
    ExistingTorrent,
    TorrentClientConfig,
    TorrentClientError,
)
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import DeliveryJob, MonitorItem, ReleaseVersion
from torrwatch.domain.enums import DeliveryStatus, TorrentClientType
from torrwatch.services.delivery import DeliveryRepository, DeliveryService, TorrentClientRepository
from torrwatch.services.jobs import MonitorRepository, now_utc
from torrwatch.worker.main import execute_claimed_delivery


class FakeAdapter:
    def __init__(self, *, old_present: bool = True, new_present: bool = False) -> None:
        self.present = {"old": old_present, "new": new_present}
        self.calls: list[str] = []
        self.fail_add = False
        self.fail_remove = False

    async def inspect(self, infohash: str) -> ExistingTorrent | None:
        self.calls.append(f"inspect:{infohash}")
        return ExistingTorrent(infohash) if self.present.get(infohash) else None

    async def add(self, _: bytes, infohash: str, options: AddOptions) -> AddResult:
        self.calls.append(f"add:{infohash}")
        assert options.save_path == "/downloads"
        if self.fail_add:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "client unavailable")
        self.present[infohash] = True
        return AddResult(infohash)

    async def remove(self, infohash: str, *, delete_data: bool = False) -> None:
        self.calls.append(f"remove:{infohash}:{delete_data}")
        assert delete_data is False
        if self.fail_remove:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "client unavailable")
        self.present[infohash] = False


def _setup(settings: object) -> tuple[Database, DeliveryService, DeliveryRepository, int]:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    monitor = MonitorRepository(database).create(
        name="delivery",
        url="https://rutracker.org/forum/viewtopic.php?t=77",
        plugin_id="rutracker",
        next_check_at=now_utc(),
    )
    with database.session() as session:
        stored = session.get(MonitorItem, monitor.id)
        assert stored is not None
        stored.client_save_path = "/downloads"
        old = ReleaseVersion(monitor_id=monitor.id, infohash_v1="old", idempotency_key="old")
        session.add(old)
        session.flush()
        artifact = Path(settings.data_dir) / "new.torrent"  # type: ignore[union-attr]
        artifact.write_bytes(b"validated fixture")
        new = ReleaseVersion(
            monitor_id=monitor.id,
            infohash_v1="new",
            file_path=str(artifact),
            idempotency_key="new",
        )
        session.add(new)
        session.flush()
        release_id = new.id
    secrets = SecretBox(settings.resolved_master_key_file)  # type: ignore[union-attr]
    client = TorrentClientRepository(database, secrets).create(
        TorrentClientConfig(
            0,
            "local",
            TorrentClientType.QBITTORRENT,
            "http://192.168.1.50:8080",
            "u",
            "password",
        )
    )
    repository = DeliveryRepository(database)
    assert repository.enqueue(release_id, client.id)
    return database, DeliveryService(database, secrets), repository, release_id


@pytest.mark.asyncio
async def test_delivery_adds_verifies_then_removes_old_and_completes(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, repository, release_id = _setup(settings)
    adapter = FakeAdapter()
    monkeypatch.setattr("torrwatch.services.delivery.adapter_for", lambda _: adapter)
    try:
        job = repository.claim_next("worker-a", 60)
        assert job is not None
        assert await execute_claimed_delivery(
            repository, service, job, "worker-a", lease_seconds=60, renewal_seconds=1
        )
        assert adapter.calls == [
            "inspect:new",
            "add:new",
            "inspect:new",
            "inspect:old",
            "remove:old:False",
        ]
        with database.session() as session:
            saved = session.get(DeliveryJob, job.id)
            assert saved is not None and saved.status == DeliveryStatus.SUCCESS
            assert saved.release_id == release_id
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_delivery_failure_keeps_old_and_retry_converges_after_external_add(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, repository, _ = _setup(settings)
    adapter = FakeAdapter()
    adapter.fail_add = True
    monkeypatch.setattr("torrwatch.services.delivery.adapter_for", lambda _: adapter)
    try:
        job = repository.claim_next("worker-a", 60)
        assert job is not None
        assert not await execute_claimed_delivery(
            repository, service, job, "worker-a", lease_seconds=60, renewal_seconds=1
        )
        assert adapter.present["old"] is True
        assert not any(call.startswith("remove") for call in adapter.calls)
        with database.session() as session:
            saved = session.get(DeliveryJob, job.id)
            assert saved is not None and saved.status == DeliveryStatus.FAILED_RETRYABLE
            saved.next_attempt_at = now_utc()

        # Model a response-loss crash: the client accepted the add, but local
        # completion did not happen.  Retry inspects first and only removes old.
        adapter.fail_add = False
        adapter.present["new"] = True
        adapter.calls.clear()
        retry = repository.claim_next("worker-b", 60)
        assert retry is not None
        assert await execute_claimed_delivery(
            repository, service, retry, "worker-b", lease_seconds=60, renewal_seconds=1
        )
        assert adapter.calls == ["inspect:new", "inspect:new", "inspect:old", "remove:old:False"]
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_delivery_retry_after_old_removal_is_idempotent(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, service, repository, _ = _setup(settings)
    adapter = FakeAdapter(old_present=False, new_present=True)
    monkeypatch.setattr("torrwatch.services.delivery.adapter_for", lambda _: adapter)
    try:
        job = repository.claim_next("worker-a", 60)
        assert job is not None
        assert await execute_claimed_delivery(
            repository, service, job, "worker-a", lease_seconds=60, renewal_seconds=1
        )
        assert adapter.calls == ["inspect:new", "inspect:new", "inspect:old"]
    finally:
        database.dispose()


def test_delivery_claim_ownership_lease_and_abandoned_recovery(settings: object) -> None:
    database, _, repository, _ = _setup(settings)
    try:
        first = repository.claim_next("worker-a", 60)
        assert first is not None
        assert repository.claim_next("worker-b", 60) is None
        assert repository.renew_lease(first.id, "worker-a", 60)
        assert not repository.complete(first.id, "worker-b")
        with database.session() as session:
            saved = session.get(DeliveryJob, first.id)
            assert saved is not None
            saved.lease_expires_at = now_utc() - timedelta(seconds=1)
        assert repository.recover_abandoned() == 1
        retry = repository.claim_next("worker-b", 60)
        assert retry is not None and retry.id == first.id
    finally:
        database.dispose()
