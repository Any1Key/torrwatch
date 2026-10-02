"""Durable notification queue; outbound sends never share a DB transaction."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import (
    MonitorItem,
    NotificationChannel,
    NotificationJob,
    ReleaseVersion,
    TorrentClient,
)
from torrwatch.domain.enums import NOTIFICATION_CLAIMABLE_STATUSES, NotificationStatus
from torrwatch.notifications.adapters import adapter_for
from torrwatch.notifications.repository import NotificationChannelRepository
from torrwatch.notifications.types import NotificationError, NotificationErrorCode
from torrwatch.services.jobs import now_utc

RETRY_SECONDS = (60, 300, 900, 3600, 10800)

EVENT_NOTIFICATION_TYPES = {
    "TORRENT_CHANGE_DETECTED": "UPDATE_DETECTED",
    "AUTH_REQUIRED": "TRACKER_AUTH_FAILED",
    "AUTH_FAILED": "TRACKER_AUTH_FAILED",
    "PLUGIN_PARSE_ERROR": "TRACKER_BROKEN",
    "TRACKER_UNAVAILABLE": "TRACKER_BROKEN",
    "TRACKER_NETWORK_ERROR": "TRACKER_BROKEN",
    "TRACKER_RATE_LIMITED": "TRACKER_BROKEN",
    "PROXY_ERROR": "TRACKER_BROKEN",
    "INVALID_TORRENT_RESPONSE": "TRACKER_BROKEN",
}


def notification_payload(
    *, event_type: str, message: str, monitor_id: int | None = None, release_id: int | None = None
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": event_type,
        "message": message,
        "monitor_id": monitor_id,
        "release_id": release_id,
        "timestamp": now_utc().isoformat(),
    }


class NotificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue_for_event(
        self, event_type: str, payload: dict[str, object], event_id: int | None = None
    ) -> int:
        now = now_utc()
        queued = 0
        with self.database.session() as session:
            channels = session.scalars(
                select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
            ).all()
            for channel in channels:
                if event_type not in json.loads(channel.event_types_json):
                    continue
                key = f"notification:{channel.id}:{event_id if event_id is not None else event_type}:{payload.get('release_id', '')}"
                result = session.execute(
                    insert(NotificationJob)
                    .values(
                        channel_id=channel.id,
                        event_id=event_id,
                        event_type=event_type,
                        payload_json=json.dumps(payload),
                        status=NotificationStatus.PENDING,
                        active_key=key,
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[NotificationJob.active_key])
                )
                queued += int(getattr(result, "rowcount", 0) or 0)
        return queued

    def enqueue_for_monitor_event(
        self,
        event_id: int,
        event_code: str,
        monitor_id: int,
        message: str,
        release_id: int | None = None,
    ) -> int:
        """Translate an internal monitor event into configured notifications."""
        event_type = EVENT_NOTIFICATION_TYPES.get(event_code)
        if event_type is None:
            return 0
        with self.database.session() as session:
            monitor = session.get(MonitorItem, monitor_id)
            release = session.get(ReleaseVersion, release_id) if release_id else None
            client = (
                session.get(TorrentClient, monitor.torrent_client_id)
                if monitor is not None and monitor.torrent_client_id is not None
                else None
            )
            if monitor is None:
                return 0
            payload = notification_payload(
                event_type=event_type,
                message=message,
                monitor_id=monitor.id,
                release_id=release.id if release else None,
            )
            payload.update(
                {
                    "monitor_name": monitor.name,
                    "external_tracker_id": monitor.external_tracker_id,
                    "torrent_name": release.torrent_name if release else None,
                    "size_bytes": release.total_size if release else None,
                    "file_count": release.file_count if release else None,
                    "client_name": client.name if client else None,
                    "client_save_path": monitor.client_save_path,
                }
            )
        return self.enqueue_for_event(event_type, payload, event_id)

    def claim_next(self, worker_id: str, lease_seconds: int) -> NotificationJob | None:
        now = now_utc()
        with self.database.session() as session:
            ident = session.scalar(
                select(NotificationJob.id)
                .where(
                    NotificationJob.status.in_(NOTIFICATION_CLAIMABLE_STATUSES),
                    NotificationJob.next_attempt_at <= now,
                )
                .order_by(NotificationJob.next_attempt_at, NotificationJob.id)
                .limit(1)
            )
            if ident is None:
                return None
            result = session.execute(
                update(NotificationJob)
                .where(
                    NotificationJob.id == ident,
                    NotificationJob.status.in_(NOTIFICATION_CLAIMABLE_STATUSES),
                )
                .values(
                    status=NotificationStatus.RUNNING,
                    worker_id=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempts=NotificationJob.attempts + 1,
                )
            )
            return (
                session.get(NotificationJob, ident)
                if int(getattr(result, "rowcount", 0) or 0)
                else None
            )

    def renew_lease(self, ident: int, worker_id: str, lease_seconds: int) -> bool:
        with self.database.session() as s:
            r = s.execute(
                update(NotificationJob)
                .where(
                    NotificationJob.id == ident,
                    NotificationJob.status == NotificationStatus.RUNNING,
                    NotificationJob.worker_id == worker_id,
                )
                .values(lease_expires_at=now_utc() + timedelta(seconds=lease_seconds))
            )
            return bool(getattr(r, "rowcount", 0))

    def finish(self, ident: int, worker_id: str, error: NotificationError | None = None) -> bool:
        with self.database.session() as s:
            job = s.get(NotificationJob, ident)
            if (
                job is None
                or job.status != NotificationStatus.RUNNING
                or job.worker_id != worker_id
            ):
                return False
            if error is None:
                job.status, job.completed_at, job.active_key = (
                    NotificationStatus.SUCCESS,
                    now_utc(),
                    None,
                )
            elif error.code in {
                NotificationErrorCode.AUTH_FAILED,
                NotificationErrorCode.DESTINATION_INVALID,
                NotificationErrorCode.INVALID_CONFIGURATION,
                NotificationErrorCode.PROTOCOL_ERROR,
            }:
                job.status, job.completed_at, job.active_key, job.last_error = (
                    NotificationStatus.FAILED_PERMANENT,
                    now_utc(),
                    None,
                    str(error),
                )
            else:
                delay = (
                    error.retry_after
                    or RETRY_SECONDS[min(job.attempts - 1, len(RETRY_SECONDS) - 1)]
                )
                job.status, job.lease_expires_at, job.next_attempt_at, job.last_error = (
                    NotificationStatus.FAILED_RETRYABLE,
                    None,
                    now_utc() + timedelta(seconds=delay),
                    str(error),
                )
            return True

    def recover_abandoned(self) -> int:
        with self.database.session() as s:
            r = s.execute(
                update(NotificationJob)
                .where(
                    NotificationJob.status == NotificationStatus.RUNNING,
                    NotificationJob.lease_expires_at < now_utc(),
                )
                .values(
                    status=NotificationStatus.FAILED_RETRYABLE,
                    worker_id=None,
                    lease_expires_at=None,
                    next_attempt_at=now_utc(),
                    last_error="Notification lease expired; job recovered.",
                )
            )
            return int(getattr(r, "rowcount", 0) or 0)


class NotificationService:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.jobs = NotificationRepository(database)
        self.channels = NotificationChannelRepository(database, secrets)

    async def send(self, job: NotificationJob, worker_id: str) -> bool:
        try:
            await adapter_for(self.channels.config(job.channel_id)).send(
                json.loads(job.payload_json)
            )
        except NotificationError as error:
            self.jobs.finish(job.id, worker_id, error)
            return False
        return self.jobs.finish(job.id, worker_id)
