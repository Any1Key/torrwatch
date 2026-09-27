"""Durable delivery queue and safe client replacement orchestration."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert

from torrwatch.clients.adapters import (
    QBittorrentAdapter,
    TransmissionAdapter,
    validate_admin_endpoint,
)
from torrwatch.clients.types import (
    AddOptions,
    ClientErrorCode,
    TorrentClientAdapter,
    TorrentClientConfig,
    TorrentClientError,
)
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import DeliveryJob, MonitorItem, ReleaseVersion, TorrentClient
from torrwatch.domain.enums import (
    DELIVERY_CLAIMABLE_STATUSES,
    DeliveryStatus,
    TorrentClientType,
)
from torrwatch.services.jobs import now_utc

RETRY_SECONDS = (60, 300, 900, 3600, 10800)


def delivery_key(release_id: int, client_id: int) -> str:
    return f"delivery:{release_id}:{client_id}"


class DeliveryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(self, release_id: int, client_id: int) -> bool:
        now = now_utc()
        with self.database.session() as session:
            result = session.execute(
                insert(DeliveryJob)
                .values(
                    release_id=release_id,
                    client_id=client_id,
                    status=DeliveryStatus.PENDING,
                    active_key=delivery_key(release_id, client_id),
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[DeliveryJob.active_key])
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def claim_next(self, worker_id: str, lease_seconds: int) -> DeliveryJob | None:
        now = now_utc()
        with self.database.session() as session:
            candidate = session.scalar(
                select(DeliveryJob.id)
                .where(
                    DeliveryJob.status.in_(DELIVERY_CLAIMABLE_STATUSES),
                    DeliveryJob.next_attempt_at <= now,
                )
                .order_by(DeliveryJob.next_attempt_at, DeliveryJob.id)
                .limit(1)
            )
            if candidate is None:
                return None
            result = session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.id == candidate, DeliveryJob.status.in_(DELIVERY_CLAIMABLE_STATUSES)
                )
                .values(
                    status=DeliveryStatus.RUNNING,
                    worker_id=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempts=DeliveryJob.attempts + 1,
                    started_at=func.coalesce(DeliveryJob.started_at, now),
                )
            )
            return (
                session.get(DeliveryJob, candidate)
                if int(getattr(result, "rowcount", 0) or 0)
                else None
            )

    def renew_lease(self, job_id: int, worker_id: str, lease_seconds: int) -> bool:
        with self.database.session() as session:
            result = session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.id == job_id,
                    DeliveryJob.status == DeliveryStatus.RUNNING,
                    DeliveryJob.worker_id == worker_id,
                )
                .values(lease_expires_at=now_utc() + timedelta(seconds=lease_seconds))
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def complete(self, job_id: int, worker_id: str) -> bool:
        with self.database.session() as session:
            job = session.get(DeliveryJob, job_id)
            if job is None or job.status != DeliveryStatus.RUNNING or job.worker_id != worker_id:
                return False
            job.status, job.completed_at, job.lease_expires_at, job.active_key = (
                DeliveryStatus.SUCCESS,
                now_utc(),
                None,
                None,
            )
            return True

    def fail(self, job_id: int, worker_id: str, message: str, *, retryable: bool) -> bool:
        with self.database.session() as session:
            job = session.get(DeliveryJob, job_id)
            if job is None or job.status != DeliveryStatus.RUNNING or job.worker_id != worker_id:
                return False
            job.last_error, job.lease_expires_at = message, None
            if retryable:
                job.status = DeliveryStatus.FAILED_RETRYABLE
                job.next_attempt_at = now_utc() + timedelta(
                    seconds=RETRY_SECONDS[min(job.attempts - 1, len(RETRY_SECONDS) - 1)]
                )
            else:
                job.status, job.completed_at, job.active_key = (
                    DeliveryStatus.FAILED_PERMANENT,
                    now_utc(),
                    None,
                )
            return True

    def recover_abandoned(self) -> int:
        now = now_utc()
        with self.database.session() as session:
            result = session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.status == DeliveryStatus.RUNNING, DeliveryJob.lease_expires_at < now
                )
                .values(
                    status=DeliveryStatus.FAILED_RETRYABLE,
                    worker_id=None,
                    lease_expires_at=None,
                    next_attempt_at=now,
                    last_error="Delivery lease expired; job recovered.",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)


class TorrentClientRepository:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database, self.secrets = database, secrets

    def create(self, config: TorrentClientConfig) -> TorrentClient:
        endpoint = validate_admin_endpoint(config.base_url)
        encrypted = self.secrets.encrypt(config.password) if config.password else None
        with self.database.session() as session:
            client = TorrentClient(
                name=config.name,
                type=config.type,
                base_url=endpoint,
                username=config.username,
                encrypted_password=encrypted,
                default_save_path=config.default_save_path,
                default_category=config.default_category,
                default_tags_json=json.dumps(config.default_tags),
            )
            session.add(client)
            session.flush()
            return client

    def config(self, client_id: int) -> TorrentClientConfig:
        with self.database.session() as session:
            client = session.get(TorrentClient, client_id)
            if client is None or not client.enabled:
                raise TorrentClientError(
                    ClientErrorCode.INVALID_CONFIGURATION, "Torrent client is unavailable."
                )
            tags = tuple(json.loads(client.default_tags_json or "[]"))
            return TorrentClientConfig(
                client.id,
                client.name,
                TorrentClientType(client.type),
                client.base_url,
                client.username,
                self.secrets.decrypt(client.encrypted_password)
                if client.encrypted_password
                else None,
                client.default_save_path,
                client.default_category,
                tags,
            )


def adapter_for(config: TorrentClientConfig) -> TorrentClientAdapter:
    if config.type == TorrentClientType.QBITTORRENT:
        return QBittorrentAdapter(config)
    if config.type == TorrentClientType.TRANSMISSION:
        return TransmissionAdapter(config)
    raise TorrentClientError(
        ClientErrorCode.INVALID_CONFIGURATION, "Unsupported torrent client type."
    )


class DeliveryService:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database, self.clients, self.jobs = (
            database,
            TorrentClientRepository(database, secrets),
            DeliveryRepository(database),
        )

    async def deliver(self, job: DeliveryJob, worker_id: str) -> bool:
        missing_artifact = False
        with self.database.session() as session:
            release = session.get(ReleaseVersion, job.release_id)
            monitor = session.get(MonitorItem, release.monitor_id) if release else None
            if (
                release is None
                or monitor is None
                or monitor.deleted_at is not None
                or not release.file_path
            ):
                missing_artifact = True
            else:
                previous = session.scalar(
                    select(ReleaseVersion)
                    .where(ReleaseVersion.monitor_id == monitor.id, ReleaseVersion.id != release.id)
                    .order_by(ReleaseVersion.detected_at.desc(), ReleaseVersion.id.desc())
                    .limit(1)
                )
                old_hash = (previous.infohash_v1 or previous.infohash_v2) if previous else None
                options = AddOptions(
                    monitor.client_save_path,
                    monitor.category,
                    tuple(json.loads(monitor.tags_json or "[]")),
                )
                file_path = release.file_path
                new_hash = release.infohash_v1 or release.infohash_v2
        if missing_artifact:
            self.jobs.fail(
                job.id, worker_id, "Validated release artifact is unavailable.", retryable=False
            )
            return False
        if not new_hash:
            self.jobs.fail(
                job.id, worker_id, "Validated release has no torrent identity.", retryable=False
            )
            return False
        try:
            # File I/O, as well as every client API call below, happens after
            # the short snapshot transaction has committed.
            payload = Path(file_path).read_bytes()
            adapter = adapter_for(self.clients.config(job.client_id))
            if await adapter.inspect(new_hash) is None:
                await adapter.add(payload, new_hash, options)
            if await adapter.inspect(new_hash) is None:
                raise TorrentClientError(
                    ClientErrorCode.PROTOCOL_ERROR, "Torrent client did not verify the new torrent."
                )
            if old_hash and old_hash != new_hash and await adapter.inspect(old_hash) is not None:
                await adapter.remove(old_hash, delete_data=False)
        except TorrentClientError as error:
            self.jobs.fail(
                job.id,
                worker_id,
                str(error),
                retryable=error.code
                not in {ClientErrorCode.AUTH_FAILED, ClientErrorCode.INVALID_CONFIGURATION},
            )
            return False
        return self.jobs.complete(job.id, worker_id)
