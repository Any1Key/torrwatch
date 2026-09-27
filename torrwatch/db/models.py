"""Phase 0 persistence models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from torrwatch.domain.enums import (
    DeliveryStatus,
    InitialSyncMode,
    JobStatus,
    JobType,
    MonitorStatus,
    NotificationChannelType,
    NotificationStatus,
    ProxyFallbackMode,
    ProxyType,
    TorrentClientType,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite stores naive values; this boundary always reads and writes UTC-aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC timestamp must be timezone-aware.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class WorkerState(Base):
    __tablename__ = "worker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MonitorItem(Base):
    __tablename__ = "monitor_items"
    __table_args__ = (Index("ix_monitor_items_schedule", "enabled", "paused", "next_check_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    external_tracker_id: Mapped[str | None] = mapped_column(String(255))
    plugin_id: Mapped[str | None] = mapped_column(String(128))
    tracker_account_id: Mapped[int | None] = mapped_column(Integer)
    proxy_override_id: Mapped[int | None] = mapped_column(Integer)
    torrent_client_id: Mapped[int | None] = mapped_column(Integer)
    client_save_path: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(255))
    tags_json: Mapped[str | None] = mapped_column(Text)
    initial_sync_mode: Mapped[InitialSyncMode] = mapped_column(
        String(32), default=InitialSyncMode.BASELINE_ONLY
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)
    last_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_update_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    current_release_id: Mapped[int | None] = mapped_column(Integer)
    current_infohash_v1: Mapped[str | None] = mapped_column(String(64))
    current_infohash_v2: Mapped[str | None] = mapped_column(String(128))
    current_status: Mapped[MonitorStatus] = mapped_column(String(32), default=MonitorStatus.HEALTHY)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ReleaseVersion(Base):
    __tablename__ = "release_versions"
    __table_args__ = (
        UniqueConstraint("monitor_id", "idempotency_key", name="uq_release_monitor_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitor_items.id"), nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    version_key: Mapped[str | None] = mapped_column(String(512))
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    infohash_v1: Mapped[str | None] = mapped_column(String(64))
    infohash_v2: Mapped[str | None] = mapped_column(String(128))
    torrent_sha256: Mapped[str | None] = mapped_column(String(64))
    torrent_name: Mapped[str | None] = mapped_column(String(1024))
    total_size: Mapped[int | None] = mapped_column(Integer)
    file_count: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(512))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_monitor_created", "monitor_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int | None] = mapped_column(ForeignKey("monitor_items.id"), index=True)
    tracker_plugin_id: Mapped[str | None] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    event_code: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_jobs_active_key"),
        Index("ix_jobs_claimable", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[JobType] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )
    monitor_id: Mapped[int | None] = mapped_column(ForeignKey("monitor_items.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(String(32), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(255))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ProxyProfile(Base):
    __tablename__ = "proxy_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_proxy_profiles_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[ProxyType] = mapped_column(String(16), nullable=False)
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(255))
    encrypted_password: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fallback_mode: Mapped[ProxyFallbackMode] = mapped_column(
        String(16), default=ProxyFallbackMode.DISABLED, nullable=False
    )
    fallback_proxy_id: Mapped[int | None] = mapped_column(ForeignKey("proxy_profiles.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class TrackerSession(Base):
    __tablename__ = "tracker_sessions"
    __table_args__ = (UniqueConstraint("namespace", name="uq_tracker_sessions_namespace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_status: Mapped[str] = mapped_column(
        String(32), default="UNVERIFIED", server_default="UNVERIFIED"
    )
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    encrypted_cookies: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    last_successful_auth_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class SessionImportPairing(Base):
    __tablename__ = "session_import_pairings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class PluginStateRecord(Base):
    __tablename__ = "plugin_states"
    __table_args__ = (
        UniqueConstraint("plugin_id", "scope", "key", name="uq_plugin_states_namespace_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="global")
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class TorrentClient(Base):
    __tablename__ = "torrent_clients"
    __table_args__ = (UniqueConstraint("name", name="uq_torrent_clients_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[TorrentClientType] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    encrypted_password: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_save_path: Mapped[str | None] = mapped_column(Text)
    default_category: Mapped[str | None] = mapped_column(String(255))
    default_tags_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class StoragePath(Base):
    """Administrator-defined, named download destinations."""

    __tablename__ = "storage_paths"
    __table_args__ = (UniqueConstraint("name", name="uq_storage_paths_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_delivery_jobs_active_key"),
        Index("ix_delivery_jobs_claimable", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release_versions.id"), nullable=False, index=True
    )
    client_id: Mapped[int] = mapped_column(
        ForeignKey("torrent_clients.id"), nullable=False, index=True
    )
    status: Mapped[DeliveryStatus] = mapped_column(String(32), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(255))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (UniqueConstraint("name", name="uq_notification_channels_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[NotificationChannelType] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    encrypted_config: Mapped[str] = mapped_column(Text, nullable=False)
    event_types_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class NotificationJob(Base):
    __tablename__ = "notification_jobs"
    __table_args__ = (
        UniqueConstraint("active_key", name="uq_notification_jobs_active_key"),
        Index("ix_notification_jobs_claimable", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id"), nullable=False, index=True
    )
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(String(32), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(255))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
