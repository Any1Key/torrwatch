"""Short-transaction application services used by both browser and REST UI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update

from torrwatch.db.database import Database
from torrwatch.db.models import (
    DeliveryJob,
    Event,
    Job,
    MonitorItem,
    NotificationChannel,
    NotificationJob,
    ProxyProfile,
    ReleaseVersion,
    StoragePath,
    SystemSetting,
    TorrentClient,
    WorkerHeartbeat,
)
from torrwatch.domain.enums import (
    DeliveryStatus,
    InitialSyncMode,
    JobStatus,
    JobType,
    MonitorStatus,
)
from torrwatch.services.jobs import MIN_CHECK_INTERVAL_SECONDS, JobRepository, now_utc
from torrwatch.trackers.registry import PluginRegistry
from torrwatch.trackers.types import TrackerPluginError


class AdminValidationError(ValueError):
    """Safe validation failure suitable for browser and JSON clients."""


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


class AdminService:
    """Read models and enqueue-only administrative commands.

    It deliberately does not perform tracker, client, or notification network I/O.
    """

    def __init__(self, database: Database, registry: PluginRegistry) -> None:
        self.database = database
        self.registry = registry

    def dashboard(self) -> dict[str, Any]:
        since = now_utc() - timedelta(days=1)
        with self.database.session() as session:
            heartbeat = session.scalar(
                select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1)
            )
            return {
                "total_monitors": session.scalar(
                    select(func.count())
                    .select_from(MonitorItem)
                    .where(MonitorItem.deleted_at.is_(None))
                )
                or 0,
                "active_monitors": session.scalar(
                    select(func.count())
                    .select_from(MonitorItem)
                    .where(
                        MonitorItem.deleted_at.is_(None),
                        MonitorItem.enabled.is_(True),
                        MonitorItem.paused.is_(False),
                    )
                )
                or 0,
                "error_monitors": session.scalar(
                    select(func.count())
                    .select_from(MonitorItem)
                    .where(
                        MonitorItem.deleted_at.is_(None),
                        MonitorItem.current_status != MonitorStatus.HEALTHY,
                    )
                )
                or 0,
                "pending_deliveries": session.scalar(
                    select(func.count())
                    .select_from(DeliveryJob)
                    .where(DeliveryJob.status.in_(("PENDING", "RUNNING", "FAILED_RETRYABLE")))
                )
                or 0,
                "pending_checks": session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.status.in_(("PENDING", "RUNNING", "FAILED_RETRYABLE")))
                )
                or 0,
                "pending_notifications": session.scalar(
                    select(func.count())
                    .select_from(NotificationJob)
                    .where(NotificationJob.status.in_(("PENDING", "RUNNING", "FAILED_RETRYABLE")))
                )
                or 0,
                "updates_24h": session.scalar(
                    select(func.count())
                    .select_from(ReleaseVersion)
                    .where(ReleaseVersion.detected_at >= since)
                )
                or 0,
                "next_check": _timestamp(
                    session.scalar(select(func.min(MonitorItem.next_check_at)))
                ),
                "worker_heartbeat": _timestamp(heartbeat.heartbeat_at if heartbeat else None),
                "tracker_health": len(self.registry.manifests()),
                "client_health": session.scalar(
                    select(func.count())
                    .select_from(TorrentClient)
                    .where(TorrentClient.enabled.is_(True))
                )
                or 0,
            }

    def monitors(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            return [
                self._monitor_dict_with_context(session, item)
                for item in session.scalars(
                    select(MonitorItem)
                    .where(MonitorItem.deleted_at.is_(None))
                    .order_by(MonitorItem.id)
                )
            ]

    def retry_delivery(self, delivery_id: int) -> bool:
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.get(DeliveryJob, delivery_id)
            if job is None or job.status not in (
                DeliveryStatus.FAILED_RETRYABLE,
                DeliveryStatus.FAILED_PERMANENT,
            ):
                return False
            release = session.get(ReleaseVersion, job.release_id)
            monitor = session.get(MonitorItem, release.monitor_id) if release else None
            client = session.get(TorrentClient, job.client_id)
            if monitor is None or monitor.deleted_at or client is None or not client.enabled:
                return False
            key = f"delivery:{job.release_id}:{job.client_id}"
            if session.scalar(
                select(DeliveryJob.id).where(
                    DeliveryJob.active_key == key, DeliveryJob.id != job.id
                )
            ):
                return False
            job.status = DeliveryStatus.PENDING
            job.active_key = key
            job.completed_at = None
            job.worker_id = None
            job.lease_expires_at = None
            job.next_attempt_at = now_utc()
            job.last_error = None
            return True

    def monitor(self, monitor_id: int) -> dict[str, Any] | None:
        with self.database.session() as session:
            item = session.get(MonitorItem, monitor_id)
            return self._monitor_dict_with_context(session, item) if item else None

    def _monitor_dict_with_context(self, session: Any, item: MonitorItem) -> dict[str, Any]:
        result = self.monitor_dict(item)
        release = (
            session.get(ReleaseVersion, item.current_release_id)
            if item.current_release_id
            else None
        )
        client = (
            session.get(TorrentClient, item.torrent_client_id) if item.torrent_client_id else None
        )
        delivery = session.scalar(
            select(DeliveryJob)
            .join(ReleaseVersion, DeliveryJob.release_id == ReleaseVersion.id)
            .where(ReleaseVersion.monitor_id == item.id)
            .order_by(DeliveryJob.created_at.desc(), DeliveryJob.id.desc())
            .limit(1)
        )
        last_problem = session.scalar(
            select(Event)
            .where(Event.monitor_id == item.id, Event.level.in_(("WARNING", "ERROR", "CRITICAL")))
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        problem_details: dict[str, Any] = {}
        if last_problem is not None:
            try:
                loaded = json.loads(last_problem.details_json)
                if isinstance(loaded, dict):
                    problem_details = loaded
            except (TypeError, json.JSONDecodeError):
                problem_details = {}
        result.update(
            {
                "client_name": client.name if client else None,
                "client_enabled": client.enabled if client else None,
                "client_save_path": item.client_save_path,
                "initial_sync_mode": str(item.initial_sync_mode),
                "infohash_v1": item.current_infohash_v1,
                "infohash_v2": item.current_infohash_v2,
                "release_name": release.torrent_name if release else None,
                "release_size": release.total_size if release else None,
                "release_files": release.file_count if release else None,
                "release_detected_at": _timestamp(release.detected_at) if release else None,
                "delivery_status": str(delivery.status) if delivery else None,
                "delivery_error": delivery.last_error if delivery else None,
                "last_problem": last_problem.message if last_problem else None,
                "last_recommendation": problem_details.get("recommendation")
                or (self._event_recommendation(last_problem.event_code) if last_problem else None),
                "last_technical_error": problem_details.get("technical_error"),
            }
        )
        return result

    @staticmethod
    def monitor_dict(item: MonitorItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "original_url": item.original_url,
            "canonical_url": item.canonical_url,
            "plugin_id": item.plugin_id,
            "external_tracker_id": item.external_tracker_id,
            "enabled": item.enabled,
            "paused": item.paused,
            "check_interval_seconds": item.check_interval_seconds,
            "status": str(item.current_status),
            "next_check_at": _timestamp(item.next_check_at),
            "last_check_at": _timestamp(item.last_check_at),
            "last_success_at": _timestamp(item.last_success_at),
            "last_update_at": _timestamp(item.last_update_at),
            "current_release_id": item.current_release_id,
            "consecutive_failures": item.consecutive_failures,
        }

    def create_monitor(
        self, name: str = "", url: str = "", interval_seconds: int = 1800
    ) -> dict[str, Any]:
        if len(name) > 255:
            raise AdminValidationError("Укажите имя монитора длиной до 255 символов.")
        if interval_seconds < MIN_CHECK_INTERVAL_SECONDS:
            raise AdminValidationError(
                f"Интервал не может быть меньше {MIN_CHECK_INTERVAL_SECONDS} секунд."
            )
        try:
            plugin, target = self.registry.resolve(url.strip())
        except TrackerPluginError as error:
            raise AdminValidationError("URL не поддерживается доступным tracker plugin.") from error
        with self.database.session() as session:
            item = MonitorItem(
                name=name.strip()
                or f"{plugin.manifest.display_name} #{target.external_id or 'monitor'}",
                original_url=url.strip(),
                canonical_url=target.canonical_url,
                plugin_id=plugin.manifest.id,
                external_tracker_id=target.external_id,
                initial_sync_mode=InitialSyncMode.BASELINE_AND_DELIVERY,
                check_interval_seconds=interval_seconds,
                next_check_at=now_utc(),
                current_status=MonitorStatus.HEALTHY,
            )
            session.add(item)
            session.flush()
            return self.monitor_dict(item)

    def update_monitor(
        self, monitor_id: int, *, name: str, interval_seconds: int, paused: bool
    ) -> dict[str, Any]:
        if not name.strip() or interval_seconds < MIN_CHECK_INTERVAL_SECONDS:
            raise AdminValidationError("Недопустимые параметры монитора.")
        with self.database.session() as session:
            item = session.get(MonitorItem, monitor_id)
            if item is None:
                raise AdminValidationError("Монитор не найден.")
            item.name, item.check_interval_seconds, item.paused = (
                name.strip(),
                interval_seconds,
                paused,
            )
            item.current_status = MonitorStatus.PAUSED if paused else MonitorStatus.HEALTHY
            return self.monitor_dict(item)

    def enqueue_check(self, monitor_id: int) -> bool:
        with self.database.session() as session:
            if (
                session.scalar(
                    select(MonitorItem).where(
                        MonitorItem.id == monitor_id, MonitorItem.deleted_at.is_(None)
                    )
                )
                is None
            ):
                raise AdminValidationError("Монитор не найден.")
        return JobRepository(self.database).enqueue_monitor_check(monitor_id, now_utc())

    def force_check(self, monitor_id: int) -> bool:
        """Run a manual check now, even when an automatic retry is scheduled."""
        if self.enqueue_check(monitor_id):
            return True
        now = now_utc()
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(Job)
                .where(
                    Job.monitor_id == monitor_id,
                    Job.job_type == JobType.MONITOR_CHECK,
                    Job.status.in_((JobStatus.PENDING, JobStatus.FAILED_RETRYABLE)),
                )
                .order_by(Job.id.desc())
            )
            if job is None:
                return False
            job.status = JobStatus.PENDING
            job.attempts = 0
            job.next_attempt_at = now
            job.last_error = None
            job.worker_id = None
            job.lease_expires_at = None
            job.completed_at = None
            monitor = session.get(MonitorItem, monitor_id)
            if monitor is not None:
                monitor.next_check_at = now
            return True

    def delete_monitor(self, monitor_id: int, *, remove_from_client: bool = False) -> bool:
        """Archive a monitor and cancel queued checks while retaining history/artifacts."""
        with self.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            item = session.get(MonitorItem, monitor_id)
            if item is None or item.deleted_at is not None:
                return False
            releases = select(ReleaseVersion.id).where(ReleaseVersion.monitor_id == monitor_id)
            if session.scalar(
                select(Job.id).where(Job.monitor_id == monitor_id, Job.status == JobStatus.RUNNING)
            ) or session.scalar(
                select(DeliveryJob.id).where(
                    DeliveryJob.release_id.in_(releases),
                    DeliveryJob.status == DeliveryStatus.RUNNING,
                )
            ):
                raise AdminValidationError(
                    "Дождитесь завершения текущей проверки или доставки и повторите удаление."
                )
            if remove_from_client:
                identity = item.current_infohash_v1 or item.current_infohash_v2
                if not identity or not item.torrent_client_id:
                    raise AdminValidationError(
                        "Нет подтверждённой версии или выбранного клиента для удаления."
                    )
                client = session.get(TorrentClient, item.torrent_client_id)
                if client is None or not client.enabled:
                    raise AdminValidationError(
                        "Клиент выключен или недоступен. Проверьте настройки."
                    )
                if session.scalar(
                    select(MonitorItem.id).where(
                        MonitorItem.id != item.id,
                        MonitorItem.deleted_at.is_(None),
                        MonitorItem.torrent_client_id == item.torrent_client_id,
                        (MonitorItem.current_infohash_v1 == identity)
                        | (MonitorItem.current_infohash_v2 == identity),
                    )
                ):
                    raise AdminValidationError(
                        "Эту раздачу использует другой торрент TorrWatch. Можно удалить только из списка."
                    )
                session.add(
                    Job(
                        job_type=JobType.CLIENT_REMOVE,
                        monitor_id=monitor_id,
                        payload_json=json.dumps(
                            {
                                "client_id": item.torrent_client_id,
                                "infohash": identity,
                                "endpoint": client.base_url,
                            }
                        ),
                        status=JobStatus.PENDING,
                        active_key=f"client-remove:{monitor_id}",
                        next_attempt_at=now_utc(),
                    )
                )
            session.execute(
                update(DeliveryJob)
                .where(
                    DeliveryJob.release_id.in_(releases),
                    DeliveryJob.status.in_(
                        (DeliveryStatus.PENDING, DeliveryStatus.FAILED_RETRYABLE)
                    ),
                )
                .values(
                    status=DeliveryStatus.FAILED_PERMANENT,
                    active_key=None,
                    completed_at=now_utc(),
                    last_error="Торрент удалён из TorrWatch; доставка отменена.",
                )
            )
            item.deleted_at = now_utc()
            item.enabled = False
            item.paused = True
            for job in session.scalars(
                select(Job).where(
                    Job.monitor_id == monitor_id,
                    Job.job_type == JobType.MONITOR_CHECK,
                    Job.status.in_((JobStatus.PENDING, JobStatus.FAILED_RETRYABLE)),
                )
            ):
                job.status = JobStatus.FAILED_PERMANENT
                job.active_key = None
                job.completed_at = now_utc()
            session.add(
                Event(
                    monitor_id=monitor_id,
                    level="INFO",
                    event_code="MONITOR_DELETED",
                    message="Monitor removed from active scheduling.",
                    details_json="{}",
                    created_at=now_utc(),
                )
            )
            return True

    def timeline(self, monitor_id: int) -> list[dict[str, Any]]:
        with self.database.session() as session:
            if session.get(MonitorItem, monitor_id) is None:
                return []
            events = session.scalars(
                select(Event)
                .where(Event.monitor_id == monitor_id)
                .order_by(Event.created_at.desc(), Event.id.desc())
            )
            return [
                {
                    "id": event.id,
                    "code": event.event_code,
                    "level": event.level,
                    "message": event.message,
                    "details": self._event_details(event),
                    "created_at": _timestamp(event.created_at),
                }
                for event in events
            ]

    @staticmethod
    def _safe_event_details(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _event_recommendation(code: str) -> str | None:
        """Provide guidance for legacy events without structured details."""
        return {
            "TRACKER_UNAVAILABLE": "Проверьте доступность трекера, Cookie и User-Agent. При HTTP 403 импортируйте свежую сессию в разделе «Трекеры и сессии».",
            "AUTH_REQUIRED": "Войдите на трекер в браузере и импортируйте свежие Cookie.",
            "AUTH_FAILED": "Проверьте Cookie и User-Agent в разделе «Трекеры и сессии».",
            "PROXY_ERROR": "Проверьте адрес, порт и авторизацию прокси либо отключите прокси-профиль.",
            "TEMPORARY_NETWORK_ERROR": "Проверьте DNS и сетевой доступ контейнера к трекеру, затем повторите проверку.",
            "RATE_LIMITED": "Слишком много запросов. Подождите и увеличьте интервал проверок.",
        }.get(code)

    def _event_details(self, event: Event) -> dict[str, Any]:
        details = self._safe_event_details(event.details_json)
        if not details.get("recommendation"):
            recommendation = self._event_recommendation(event.event_code)
            if recommendation:
                details["recommendation"] = recommendation
        return details

    def events(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            return [
                {
                    "id": event.id,
                    "monitor_id": event.monitor_id,
                    "plugin_id": event.tracker_plugin_id,
                    "code": event.event_code,
                    "level": event.level,
                    "message": event.message,
                    "details": self._event_details(event),
                    "created_at": _timestamp(event.created_at),
                }
                for event in session.scalars(
                    select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(200)
                )
            ]

    def filtered_events(
        self, *, monitor_id: int | None = None, level: str = "", query: str = ""
    ) -> list[dict[str, Any]]:
        rows = self.events()
        normalized_level = level.strip().upper()
        needle = query.strip().casefold()
        return [
            row
            for row in rows
            if (monitor_id is None or row["monitor_id"] == monitor_id)
            and (not normalized_level or row["level"] == normalized_level)
            and (
                not needle
                or needle in row["message"].casefold()
                or needle in row["code"].casefold()
            )
        ]

    def resource(self, name: str) -> list[dict[str, Any]]:
        mappings: dict[str, tuple[type[Any], tuple[str, ...]]] = {
            "jobs": (
                Job,
                (
                    "id",
                    "job_type",
                    "monitor_id",
                    "status",
                    "attempts",
                    "next_attempt_at",
                    "last_error",
                ),
            ),
            "paths": (StoragePath, ("id", "name", "path", "enabled")),
            "proxies": (
                ProxyProfile,
                ("id", "name", "type", "host", "port", "enabled", "fallback_mode"),
            ),
            "clients": (
                TorrentClient,
                (
                    "id",
                    "name",
                    "type",
                    "base_url",
                    "enabled",
                    "default_save_path",
                    "default_category",
                ),
            ),
            "notifications": (
                NotificationChannel,
                ("id", "name", "type", "enabled", "event_types_json", "created_at"),
            ),
            "deliveries": (
                DeliveryJob,
                (
                    "id",
                    "release_id",
                    "client_id",
                    "status",
                    "attempts",
                    "next_attempt_at",
                    "last_error",
                ),
            ),
            "notification-jobs": (
                NotificationJob,
                (
                    "id",
                    "channel_id",
                    "event_type",
                    "status",
                    "attempts",
                    "next_attempt_at",
                    "last_error",
                ),
            ),
        }
        if name not in mappings:
            raise AdminValidationError("Неизвестный ресурс.")
        model, fields = mappings[name]
        with self.database.session() as session:
            rows = session.scalars(select(model).order_by(model.id)).all()
            return [
                {
                    field: _timestamp(value)
                    if isinstance(value := getattr(row, field), datetime)
                    else str(value)
                    if hasattr(value, "value")
                    else value
                    for field in fields
                }
                for row in rows
            ]

    def trackers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": manifest.id,
                "name": manifest.display_name,
                "version": manifest.version,
                "domains": list(manifest.domains),
                "authentication": [str(item) for item in manifest.auth_modes],
            }
            for manifest in self.registry.manifests()
        ]

    def settings(self) -> list[dict[str, str]]:
        with self.database.session() as session:
            return [
                {"key": item.key, "value": item.value}
                for item in session.scalars(select(SystemSetting).order_by(SystemSetting.key))
            ]

    def system(self) -> dict[str, Any]:
        with self.database.session() as session:
            jobs = (
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.status.in_((JobStatus.PENDING, JobStatus.FAILED_RETRYABLE)))
                )
                or 0
            )
        return {
            "dashboard": self.dashboard(),
            "pending_monitor_jobs": jobs,
            "plugins": self.trackers(),
        }
