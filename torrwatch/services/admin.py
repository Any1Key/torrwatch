"""Short-transaction application services used by both browser and REST UI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

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
from torrwatch.domain.enums import JobStatus, MonitorStatus
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
                "total_monitors": session.scalar(select(func.count()).select_from(MonitorItem))
                or 0,
                "active_monitors": session.scalar(
                    select(func.count())
                    .select_from(MonitorItem)
                    .where(MonitorItem.enabled.is_(True), MonitorItem.paused.is_(False))
                )
                or 0,
                "error_monitors": session.scalar(
                    select(func.count())
                    .select_from(MonitorItem)
                    .where(MonitorItem.current_status != MonitorStatus.HEALTHY)
                )
                or 0,
                "pending_deliveries": session.scalar(
                    select(func.count())
                    .select_from(DeliveryJob)
                    .where(DeliveryJob.status.in_(("PENDING", "RUNNING", "FAILED_RETRYABLE")))
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
                self.monitor_dict(item)
                for item in session.scalars(select(MonitorItem).order_by(MonitorItem.id))
            ]

    def monitor(self, monitor_id: int) -> dict[str, Any] | None:
        with self.database.session() as session:
            item = session.get(MonitorItem, monitor_id)
            return self.monitor_dict(item) if item else None

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
            if session.get(MonitorItem, monitor_id) is None:
                raise AdminValidationError("Монитор не найден.")
        return JobRepository(self.database).enqueue_monitor_check(monitor_id, now_utc())

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
                    "created_at": _timestamp(event.created_at),
                }
                for event in events
            ]

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
                    "created_at": _timestamp(event.created_at),
                }
                for event in session.scalars(
                    select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(200)
                )
            ]

    def resource(self, name: str) -> list[dict[str, Any]]:
        mappings: dict[str, tuple[type[Any], tuple[str, ...]]] = {
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
