"""Offline acceptance of browser commands, leased execution and safe removal."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from alembic import command
from tests.integration.test_phase9_web import _login
from torrwatch.clients.types import (
    ClientErrorCode,
    ConnectionResult,
    ExistingTorrent,
    TorrentClientError,
)
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.migrations import alembic_config
from torrwatch.db.models import (
    DeliveryJob,
    Job,
    MonitorItem,
    ReleaseVersion,
    TorrentClient,
    TrackerSession,
)
from torrwatch.domain.enums import DeliveryStatus, JobStatus
from torrwatch.services.admin import AdminService, AdminValidationError
from torrwatch.services.admin_jobs import AdminJobService, enqueue_client_test
from torrwatch.services.configuration import ConfigurationService
from torrwatch.services.jobs import JobRepository, now_utc
from torrwatch.worker.main import execute_claimed_job, worker_cycle


def setup(client):
    db = client.app.state.database
    admin = AdminService(db, client.app.state.plugin_registry)
    secrets = SecretBox(client.app.state.settings.resolved_master_key_file)
    config = ConfigurationService(admin, secrets)
    ident = config.save(
        "clients",
        {
            "name": "Offline",
            "type": "TRANSMISSION",
            "url": "http://192.168.1.2:9091",
            "password": "offline-test-secret",
            "enabled": "on",
        },
    )
    monitor = admin.create_monitor(url="https://rutracker.org/forum/viewtopic.php?t=123")
    with db.session() as s:
        item = s.get(MonitorItem, monitor["id"])
        item.torrent_client_id = ident
        item.next_check_at = now_utc() + timedelta(days=1)
        release = ReleaseVersion(
            monitor_id=item.id, infohash_v1="a" * 40, file_path="/not-read-in-tests"
        )
        s.add(release)
        s.flush()
        item.current_release_id = release.id
        item.current_infohash_v1 = release.infohash_v1
        release_id = release.id
    return db, admin, secrets, ident, monitor["id"], release_id


class FakeClient:
    def __init__(self):
        self.present = True
        self.removals = 0
        self.calls = []

    async def test_connection(self):
        self.calls.append("test")
        return ConnectionResult(True)

    async def inspect(self, identity):
        self.calls.append("inspect")
        return ExistingTorrent(identity) if self.present else None

    async def remove(self, identity, *, delete_data):
        assert delete_data is False
        self.calls.append("remove")
        self.present = False
        self.removals += 1


def test_browser_commands_are_authenticated_csrf_protected_and_enqueue_only(client, monkeypatch):
    db, admin, secrets, ident, monitor, release = setup(client)
    assert client.post(f"/clients/{ident}/test").status_code == 401
    token = _login(client)
    assert client.post(f"/clients/{ident}/test").status_code == 403

    def forbid(*args):
        pytest.fail("HTTP action instantiated a client adapter")

    monkeypatch.setattr("torrwatch.services.admin_jobs.adapter_for", forbid)
    for _ in range(2):
        response = client.post(f"/clients/{ident}/test", data={"csrf": token})
        assert response.status_code == 200 and "Проверка подключения" in response.text
    with db.session() as s:
        assert len(list(s.scalars(select(Job)))) == 1
    for _ in range(2):
        response = client.post(
            f"/torrents/{monitor}/check",
            data={"csrf": token},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 202
    assert response.json()["queued"] is False
    assert "уже" in response.json()["message"]
    assert "Проверка поставлена в очередь" not in client.get("/queues").text
    assert "Подтверждаю" in client.get(f"/torrents/{monitor}/delete").text
    client.post(f"/torrents/{monitor}/delete", data={"csrf": token})
    with db.session() as s:
        assert s.get(MonitorItem, monitor).deleted_at is None
    assert "offline-test-secret" not in client.get("/queues").text


@pytest.mark.asyncio
async def test_client_test_uses_shared_lease_queue(client, monkeypatch):
    db, admin, secrets, ident, monitor, release = setup(client)
    fake = FakeClient()
    monkeypatch.setattr("torrwatch.services.admin_jobs.adapter_for", lambda config: fake)
    assert enqueue_client_test(db, ident)
    assert not enqueue_client_test(db, ident)
    jobs = JobRepository(db)
    job = jobs.claim_next("one", now_utc(), 10)
    assert jobs.claim_next("two", now_utc()) is None
    service = AdminJobService(db, secrets)
    assert await execute_claimed_job(
        jobs, job, "one", lambda j: service.handle(j, "one"), lease_seconds=10, renewal_seconds=1
    )
    assert fake.calls == ["test"]
    with db.session() as s:
        assert s.get(Job, job.id).status == JobStatus.SUCCESS
        assert s.get(MonitorItem, monitor).last_check_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("already_absent", [False, True])
async def test_remove_preserves_files_and_retry_converges(client, monkeypatch, already_absent):
    db, admin, secrets, ident, monitor, release = setup(client)
    assert admin.delete_monitor(monitor, remove_from_client=True)
    fake = FakeClient()
    fake.present = not already_absent
    monkeypatch.setattr("torrwatch.services.admin_jobs.adapter_for", lambda config: fake)
    jobs = JobRepository(db)
    job = jobs.claim_next("one", now_utc())
    service = AdminJobService(db, secrets)
    await service.handle(job, "one")  # external action succeeds; simulate crash before commit
    with db.session() as s:
        s.get(Job, job.id).lease_expires_at = now_utc() - timedelta(seconds=1)
    assert jobs.recover_abandoned(now_utc()) == 1
    retry = jobs.claim_next("two", now_utc())
    assert await execute_claimed_job(
        jobs, retry, "two", lambda j: service.handle(j, "two"), lease_seconds=10, renewal_seconds=1
    )
    assert fake.removals == (0 if already_absent else 1)
    with db.session() as s:
        assert s.get(ReleaseVersion, release) is not None
        assert s.get(MonitorItem, monitor).last_success_at is None


def test_archive_cancels_pending_delivery_and_refuses_running_work(client):
    db, admin, secrets, ident, monitor, release = setup(client)
    with db.session() as s:
        delivery = DeliveryJob(
            release_id=release,
            client_id=ident,
            status=DeliveryStatus.RUNNING,
            next_attempt_at=now_utc(),
        )
        s.add(delivery)
        s.flush()
        delivery_id = delivery.id
    with pytest.raises(AdminValidationError):
        admin.delete_monitor(monitor, remove_from_client=True)
    with db.session() as s:
        s.get(DeliveryJob, delivery_id).status = DeliveryStatus.FAILED_RETRYABLE
    assert admin.delete_monitor(monitor)
    with db.session() as s:
        assert s.get(DeliveryJob, delivery_id).status == DeliveryStatus.FAILED_PERMANENT
        assert not list(s.scalars(select(Job)))
    assert not admin.retry_delivery(delivery_id)


@pytest.mark.parametrize(
    "status", [DeliveryStatus.FAILED_RETRYABLE, DeliveryStatus.FAILED_PERMANENT]
)
def test_delivery_retry_terminal_and_active_duplicate_guards(client, status):
    db, admin, secrets, ident, monitor, release = setup(client)
    with db.session() as s:
        delivery = DeliveryJob(
            release_id=release, client_id=ident, status=status, next_attempt_at=now_utc()
        )
        s.add(delivery)
        s.flush()
        delivery_id = delivery.id
    assert admin.retry_delivery(delivery_id)
    assert not admin.retry_delivery(delivery_id)
    with db.session() as s:
        assert s.get(DeliveryJob, delivery_id).active_key == f"delivery:{release}:{ident}"


@pytest.mark.asyncio
async def test_long_operation_renewal_and_owner_loss(client, monkeypatch):
    db, admin, secrets, ident, monitor, release = setup(client)
    enqueue_client_test(db, ident)
    jobs = JobRepository(db)
    job = jobs.claim_next("one", now_utc(), 10)
    renewals = []
    original = jobs.renew_lease

    def renew(*args):
        renewals.append(1)
        return original(*args)

    monkeypatch.setattr(jobs, "renew_lease", renew)

    async def slow(job):
        await asyncio.sleep(0.05)

    assert await execute_claimed_job(jobs, job, "one", slow, lease_seconds=10, renewal_seconds=0.01)
    assert renewals
    enqueue_client_test(db, ident)
    job = jobs.claim_next("one", now_utc())

    async def lose(job):
        with db.session() as s:
            s.get(Job, job.id).worker_id = "two"
        await asyncio.sleep(0.05)

    assert not await execute_claimed_job(
        jobs, job, "one", lose, lease_seconds=10, renewal_seconds=0.01
    )
    with db.session() as s:
        assert s.get(Job, job.id).status == JobStatus.RUNNING
    stop = asyncio.Event()
    stop.set()
    assert await worker_cycle(db, "three", stop_event=stop) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,retryable", [(ClientErrorCode.AUTH_FAILED, False), (ClientErrorCode.UNAVAILABLE, True)]
)
async def test_client_errors_safe_and_classified(client, monkeypatch, code, retryable):
    db, admin, secrets, ident, monitor, release = setup(client)
    fake = FakeClient()

    async def fail():
        raise TorrentClientError(code, "do-not-leak-password")

    fake.test_connection = fail
    monkeypatch.setattr("torrwatch.services.admin_jobs.adapter_for", lambda config: fake)
    enqueue_client_test(db, ident)
    jobs = JobRepository(db)
    job = jobs.claim_next("one", now_utc())
    service = AdminJobService(db, secrets)
    assert not await execute_claimed_job(
        jobs, job, "one", lambda j: service.handle(j, "one"), lease_seconds=10, renewal_seconds=1
    )
    with db.session() as s:
        stored = s.get(Job, job.id)
        assert stored.status == (
            JobStatus.FAILED_RETRYABLE if retryable else JobStatus.FAILED_PERMANENT
        )
        assert "do-not-leak" not in stored.last_error


def test_session_import_not_auth_and_reimport_invalidates_old_result(client):
    db, admin, secrets, ident, monitor, release = setup(client)
    config = ConfigurationService(admin, secrets)
    config.save("sessions", {"plugin": "rutracker", "cookie": "sid=offline-secret"})
    with db.session() as s:
        saved = s.scalar(select(TrackerSession))
        assert saved.last_successful_auth_at is None
        saved.auth_status = "WORKING"
    assert next(r for r in config.sessions() if r["id"] == "rutracker")["status"] == "Работает"
    with db.session() as s:
        saved = s.scalar(select(TrackerSession))
        saved.auth_status = "EXPIRED"
    assert "Cookie" in next(r for r in config.sessions() if r["id"] == "rutracker")["last_error"]
    config.save("sessions", {"plugin": "rutracker", "cookie": "sid=new-offline-secret"})
    assert (
        next(r for r in config.sessions() if r["id"] == "rutracker")["status"] == "Ожидает проверки"
    )


def test_migration_admin_operations_upgrade(settings):
    settings.data_dir.mkdir(parents=True)
    command.upgrade(alembic_config(settings), "20260914_0008")
    db = Database(settings.resolved_database_url)
    with db.session() as s:
        s.execute(
            text(
                "INSERT INTO jobs (job_type,status,attempts,next_attempt_at,created_at) VALUES ('CLIENT_TEST','SUCCESS',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(alembic_config(settings), "head")
    with db.session() as s:
        assert s.scalar(select(Job.payload_json)) == "{}"
        assert s.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    db.dispose()


def test_settings_forms_not_nested_and_timezone_survives_first_save(client):
    from html.parser import HTMLParser

    class Forms(HTMLParser):
        depth = 0

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                assert self.depth == 0
                self.depth += 1

        def handle_endtag(self, tag):
            if tag == "form":
                self.depth -= 1

    token = _login(client)
    Forms().feed(client.get("/system").text)
    response = client.post("/system/settings", data={"csrf": token, "timezone": "Europe/Moscow"})
    assert response.status_code == 200
    assert client.get("/torrents").status_code == 200


def test_auth_results_respect_owner_and_cookie_generation(client):
    from torrwatch.services.monitor_checks import MonitorCheckService
    from torrwatch.transport.http import HttpTransport

    db, admin, secrets, ident, monitor, release = setup(client)
    config = ConfigurationService(admin, secrets)
    values = {"plugin": "rutracker", "cookie": "sid=offline-secret"}
    config.save("sessions", values)
    jobs = JobRepository(db)
    jobs.enqueue_monitor_check(monitor, now_utc())
    job = jobs.claim_next("owner", now_utc())
    namespace = "rutracker:account:1"
    with db.session() as s:
        imported = s.scalar(select(TrackerSession)).imported_at
    service = MonitorCheckService(db, client.app.state.settings, admin.registry, HttpTransport())
    service._record_auth(job.id, "wrong-owner", namespace, imported, "WORKING")
    with db.session() as s:
        assert s.scalar(select(TrackerSession)).auth_status == "UNVERIFIED"
    assert next(r for r in config.sessions() if r["id"] == "rutracker")["status"] == "Проверяется"
    service._record_auth(job.id, "owner", namespace, imported, "WORKING")
    with db.session() as s:
        assert s.scalar(select(TrackerSession)).last_successful_auth_at is not None
    config.save("sessions", values)
    service._record_auth(job.id, "owner", namespace, imported, "EXPIRED")
    with db.session() as s:
        assert s.scalar(select(TrackerSession)).auth_status == "UNVERIFIED"


@pytest.mark.asyncio
async def test_removal_rejects_changed_endpoint(client, monkeypatch):
    from torrwatch.services.jobs import JobExecutionFailure

    db, admin, secrets, ident, monitor, release = setup(client)
    admin.delete_monitor(monitor, remove_from_client=True)
    with db.session() as s:
        s.get(TorrentClient, ident).base_url = "http://different.example/"

    def forbid(config):
        pytest.fail("Must not use a different endpoint for confirmed removal")

    monkeypatch.setattr("torrwatch.services.admin_jobs.adapter_for", forbid)
    job = JobRepository(db).claim_next("owner", now_utc())
    with pytest.raises(JobExecutionFailure) as error:
        await AdminJobService(db, secrets).handle(job, "owner")
    assert error.value.retryable is False
