"""Generic durable monitor-check orchestration for registered tracker plugins."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from torrwatch.core.config import Settings
from torrwatch.db.database import Database
from torrwatch.db.models import DeliveryJob, Event, Job, MonitorItem, ReleaseVersion
from torrwatch.domain.enums import DeliveryStatus, InitialSyncMode, JobStatus, MonitorStatus
from torrwatch.services.jobs import JobExecutionFailure, now_utc
from torrwatch.torrent import TorrentMetadata, TorrentStore, TorrentValidationError
from torrwatch.trackers.context import PluginHttpClient, ScopedPluginSecrets, TrackerPluginContext
from torrwatch.trackers.registry import PluginNotFoundError, PluginRegistry
from torrwatch.trackers.state import PluginStateNamespace
from torrwatch.trackers.types import PluginErrorCode, RemoteReleaseState, TrackerPluginError
from torrwatch.transport.http import HttpTransport

AUTH_RETRY_SECONDS = 21_600
PARSER_RETRY_SECONDS = 10_800


@dataclass(frozen=True)
class _MonitorSnapshot:
    id: int
    original_url: str
    plugin_id: str | None
    proxy_profile_id: int | None
    tracker_account_id: int | None
    torrent_client_id: int | None
    initial_sync_mode: InitialSyncMode
    current_release_id: int | None
    current_infohash_v1: str | None
    current_infohash_v2: str | None
    current_version_key: str | None


@dataclass(frozen=True)
class _DraftRelease:
    id: int
    existing: bool


class MonitorCheckService:
    """Coordinates plugin, torrent engine and short database writes.

    The service deliberately knows no tracker-specific URL/parser behavior. The
    registry and plugin manifest supply that information, while plugin code gets
    only a scoped transport/context.
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        registry: PluginRegistry,
        transport: HttpTransport,
    ) -> None:
        self._database = database
        self._settings = settings
        self._registry = registry
        self._transport = transport
        self._store = TorrentStore(
            settings.resolved_torrents_dir, retention_count=settings.torrent_retention_count
        )

    async def handle(self, job: Job, worker_id: str) -> None:
        if job.monitor_id is None:
            raise JobExecutionFailure("Monitor check has no monitor.", retryable=False)
        snapshot = self._load_monitor(job.monitor_id)
        try:
            plugin, target = self._registry.resolve(snapshot.original_url)
        except (PluginNotFoundError, TrackerPluginError) as error:
            raise JobExecutionFailure(
                "Monitor URL is not supported by an enabled tracker plugin.",
                retryable=False,
                monitor_status=MonitorStatus.INVALID_TARGET,
                event_code="INVALID_TARGET",
            ) from error
        if snapshot.plugin_id is not None and snapshot.plugin_id != plugin.manifest.id:
            raise JobExecutionFailure(
                "Monitor plugin does not own its configured URL.",
                retryable=False,
                monitor_status=MonitorStatus.INVALID_TARGET,
                event_code="INVALID_TARGET",
            )

        scope = f"monitor:{snapshot.id}"
        state = PluginStateNamespace(self._database, plugin.manifest.id, scope)
        context = TrackerPluginContext(
            http=PluginHttpClient(
                self._transport,
                allowed_hosts=set(plugin.manifest.domains),
                session_namespace=self._session_namespace(plugin.manifest.id, snapshot),
                proxy_profile_id=snapshot.proxy_profile_id,
            ),
            state=state,
            secrets=ScopedPluginSecrets(),
        )
        try:
            remote = await plugin.check(target, context)
        except TrackerPluginError as error:
            raise self._plugin_failure(error) from error

        forced_due = self._forced_verification_due(state)
        version_changed = remote.version_key != snapshot.current_version_key
        needs_download = snapshot.current_release_id is None or version_changed or forced_due
        if not needs_download:
            self._persist_remote_check(
                job.id,
                worker_id,
                snapshot.id,
                plugin.manifest.id,
                target.canonical_url,
                target.external_id,
                remote,
                event_code="CHECK_REMOTE_UNCHANGED",
                message="Tracker release state is unchanged.",
            )
            return

        if forced_due and snapshot.current_release_id is not None and not version_changed:
            self._event_if_owned(
                job.id,
                worker_id,
                snapshot.id,
                plugin.manifest.id,
                "FORCED_VERIFICATION",
                "Periodic torrent verification started.",
            )
        try:
            payload = await plugin.download_torrent(target, remote, context)
            metadata = self._validate_payload(payload)
        except TrackerPluginError as error:
            raise self._plugin_failure(error) from error
        except TorrentValidationError as error:
            raise JobExecutionFailure(
                "Downloaded tracker response is not a valid torrent.",
                retryable=True,
                monitor_status=MonitorStatus.ERROR,
                retry_delay_seconds=PARSER_RETRY_SECONDS,
                event_code="INVALID_TORRENT_RESPONSE",
            ) from error

        if self._same_torrent(snapshot, metadata):
            self._persist_remote_check(
                job.id,
                worker_id,
                snapshot.id,
                plugin.manifest.id,
                target.canonical_url,
                target.external_id,
                remote,
                event_code=(
                    "METADATA_CHANGED_TORRENT_UNCHANGED"
                    if version_changed
                    else "FORCED_VERIFICATION_NO_CHANGE"
                ),
                message="Tracker metadata changed but torrent identity is unchanged."
                if version_changed
                else "Forced verification found the current torrent identity.",
            )
            state.set("last_torrent_verification_at", now_utc().isoformat())
            return

        draft = self._allocate_draft(job.id, worker_id, snapshot.id, remote, metadata)
        try:
            stored = self._store.store(snapshot.id, draft.id, payload)
        except (OSError, RuntimeError) as error:
            raise JobExecutionFailure(
                "Validated torrent could not be stored safely.",
                retryable=True,
                event_code="TORRENT_STORE_FAILED",
            ) from error
        created = self._finalize_release(
            job.id,
            worker_id,
            snapshot.id,
            draft.id,
            plugin.manifest.id,
            target.canonical_url,
            target.external_id,
            remote,
            metadata,
            str(stored.path),
        )
        state.set("last_torrent_verification_at", now_utc().isoformat())
        self._apply_retention(job.id, worker_id, snapshot.id, draft.id)
        if created:
            should_deliver = snapshot.current_release_id is not None or (
                snapshot.initial_sync_mode == InitialSyncMode.BASELINE_AND_DELIVERY
            )
            if should_deliver and snapshot.torrent_client_id is not None:
                from torrwatch.services.delivery import DeliveryRepository

                if DeliveryRepository(self._database).enqueue(draft.id, snapshot.torrent_client_id):
                    self._event_if_owned(
                        job.id,
                        worker_id,
                        snapshot.id,
                        plugin.manifest.id,
                        "DELIVERY_QUEUED",
                        "Validated release delivery was queued.",
                    )
            self._event_if_owned(
                job.id,
                worker_id,
                snapshot.id,
                plugin.manifest.id,
                "INITIAL_BASELINE_ESTABLISHED"
                if snapshot.current_release_id is None
                else "TORRENT_CHANGE_DETECTED",
                "Initial torrent baseline was established."
                if snapshot.current_release_id is None
                else "A new torrent identity was stored.",
            )

    @staticmethod
    def _session_namespace(plugin_id: str, snapshot: _MonitorSnapshot) -> str:
        if snapshot.tracker_account_id is not None:
            return f"{plugin_id}:account:{snapshot.tracker_account_id}"
        return f"{plugin_id}:monitor:{snapshot.id}"

    def _load_monitor(self, monitor_id: int) -> _MonitorSnapshot:
        with self._database.session() as session:
            monitor = session.get(MonitorItem, monitor_id)
            if monitor is None:
                raise JobExecutionFailure("Monitor no longer exists.", retryable=False)
            current = (
                session.get(ReleaseVersion, monitor.current_release_id)
                if monitor.current_release_id is not None
                else None
            )
            return _MonitorSnapshot(
                id=monitor.id,
                original_url=monitor.original_url,
                plugin_id=monitor.plugin_id,
                proxy_profile_id=monitor.proxy_override_id,
                tracker_account_id=monitor.tracker_account_id,
                torrent_client_id=monitor.torrent_client_id,
                initial_sync_mode=InitialSyncMode(monitor.initial_sync_mode),
                current_release_id=monitor.current_release_id,
                current_infohash_v1=monitor.current_infohash_v1,
                current_infohash_v2=monitor.current_infohash_v2,
                current_version_key=current.version_key if current is not None else None,
            )

    def _forced_verification_due(self, state: PluginStateNamespace) -> bool:
        interval = self._settings.torrent_forced_verification_seconds
        if interval == 0:
            return False
        value = state.get("last_torrent_verification_at")
        if not isinstance(value, str):
            return True
        try:
            verified_at = datetime.fromisoformat(value)
        except ValueError:
            return True
        if verified_at.tzinfo is None:
            return True
        return verified_at.astimezone(UTC) + timedelta(seconds=interval) <= now_utc()

    @staticmethod
    def _validate_payload(payload: bytes) -> TorrentMetadata:
        from torrwatch.torrent import parse_metainfo

        return parse_metainfo(payload)

    @staticmethod
    def _same_torrent(snapshot: _MonitorSnapshot, metadata: TorrentMetadata) -> bool:
        return bool(
            (snapshot.current_infohash_v1 and snapshot.current_infohash_v1 == metadata.infohash_v1)
            or (
                snapshot.current_infohash_v2
                and snapshot.current_infohash_v2 == metadata.infohash_v2
            )
        )

    @staticmethod
    def _idempotency_key(metadata: TorrentMetadata) -> str:
        if metadata.infohash_v1:
            return f"v1:{metadata.infohash_v1}"
        if metadata.infohash_v2:
            return f"v2:{metadata.infohash_v2}"
        raise ValueError("Validated torrent has no authoritative identity.")

    @staticmethod
    def _owned(session: Session, job_id: int, worker_id: str) -> bool:
        job = session.get(Job, job_id)
        return bool(
            job is not None and job.worker_id == worker_id and job.status == JobStatus.RUNNING
        )

    def _allocate_draft(
        self,
        job_id: int,
        worker_id: str,
        monitor_id: int,
        remote: RemoteReleaseState,
        metadata: TorrentMetadata,
    ) -> _DraftRelease:
        key = self._idempotency_key(metadata)
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                raise JobExecutionFailure("Job ownership was lost.", retryable=True)
            existing = session.scalar(
                select(ReleaseVersion).where(
                    ReleaseVersion.monitor_id == monitor_id, ReleaseVersion.idempotency_key == key
                )
            )
            if existing is not None:
                return _DraftRelease(existing.id, existing.file_path is not None)
            release = ReleaseVersion(
                monitor_id=monitor_id,
                version_key=remote.version_key,
                source_updated_at=remote.source_updated_at,
                infohash_v1=metadata.infohash_v1,
                infohash_v2=metadata.infohash_v2,
                torrent_sha256=metadata.torrent_sha256,
                torrent_name=metadata.name,
                total_size=metadata.total_size,
                file_count=metadata.file_count,
                metadata_json=json.dumps(
                    remote.metadata, separators=(",", ":"), ensure_ascii=False
                ),
                idempotency_key=key,
            )
            session.add(release)
            session.flush()
            return _DraftRelease(release.id, False)

    def _finalize_release(
        self,
        job_id: int,
        worker_id: str,
        monitor_id: int,
        release_id: int,
        plugin_id: str,
        canonical_url: str,
        external_id: str | None,
        remote: RemoteReleaseState,
        metadata: TorrentMetadata,
        file_path: str,
    ) -> bool:
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                raise JobExecutionFailure("Job ownership was lost.", retryable=True)
            monitor = session.get(MonitorItem, monitor_id)
            release = session.get(ReleaseVersion, release_id)
            if monitor is None or release is None:
                raise JobExecutionFailure("Monitor release state is unavailable.", retryable=True)
            already_current = monitor.current_release_id == release.id
            release.file_path = file_path
            release.version_key = remote.version_key
            release.source_updated_at = remote.source_updated_at
            release.metadata_json = json.dumps(
                remote.metadata, separators=(",", ":"), ensure_ascii=False
            )
            monitor.plugin_id = plugin_id
            monitor.canonical_url = canonical_url
            monitor.external_tracker_id = external_id
            monitor.current_release_id = release.id
            monitor.current_infohash_v1 = metadata.infohash_v1
            monitor.current_infohash_v2 = metadata.infohash_v2
            monitor.last_update_at = now_utc()
            return not already_current

    def _persist_remote_check(
        self,
        job_id: int,
        worker_id: str,
        monitor_id: int,
        plugin_id: str,
        canonical_url: str,
        external_id: str | None,
        remote: RemoteReleaseState,
        *,
        event_code: str,
        message: str,
    ) -> None:
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                raise JobExecutionFailure("Job ownership was lost.", retryable=True)
            monitor = session.get(MonitorItem, monitor_id)
            if monitor is None:
                raise JobExecutionFailure("Monitor no longer exists.", retryable=False)
            monitor.plugin_id = plugin_id
            monitor.canonical_url = canonical_url
            monitor.external_tracker_id = external_id
            session.add(
                Event(
                    monitor_id=monitor_id,
                    tracker_plugin_id=plugin_id,
                    level="INFO",
                    event_code=event_code,
                    message=message,
                    details_json="{}",
                    created_at=now_utc(),
                )
            )

    def _event_if_owned(
        self,
        job_id: int,
        worker_id: str,
        monitor_id: int,
        plugin_id: str,
        event_code: str,
        message: str,
    ) -> None:
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                return
            session.add(
                Event(
                    monitor_id=monitor_id,
                    tracker_plugin_id=plugin_id,
                    level="INFO",
                    event_code=event_code,
                    message=message,
                    details_json="{}",
                    created_at=now_utc(),
                )
            )

    def _apply_retention(
        self, job_id: int, worker_id: str, monitor_id: int, current_id: int
    ) -> None:
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                return
            release_ids = list(
                session.scalars(
                    select(ReleaseVersion.id)
                    .where(ReleaseVersion.monitor_id == monitor_id)
                    .order_by(ReleaseVersion.detected_at.desc(), ReleaseVersion.id.desc())
                )
            )
        with self._database.session() as session:
            protected_delivery_ids = set(
                session.scalars(
                    select(DeliveryJob.release_id).where(
                        DeliveryJob.status.in_(
                            (
                                DeliveryStatus.PENDING,
                                DeliveryStatus.RUNNING,
                                DeliveryStatus.FAILED_RETRYABLE,
                            )
                        )
                    )
                )
            )
        deleted = self._store.prune(
            monitor_id,
            release_ids,
            protected_release_ids={current_id, *protected_delivery_ids},
        )
        deleted_ids = [int(path.stem) for path in deleted]
        if not deleted_ids:
            return
        with self._database.session() as session:
            if not self._owned(session, job_id, worker_id):
                return
            for release_id in deleted_ids:
                release = session.get(ReleaseVersion, release_id)
                if (
                    release is not None
                    and release.monitor_id == monitor_id
                    and release.id != current_id
                ):
                    session.delete(release)

    @staticmethod
    def _plugin_failure(error: TrackerPluginError) -> JobExecutionFailure:
        mapping: dict[PluginErrorCode, tuple[bool, MonitorStatus, int | None, str]] = {
            PluginErrorCode.INVALID_TARGET: (
                False,
                MonitorStatus.INVALID_TARGET,
                None,
                "INVALID_TARGET",
            ),
            PluginErrorCode.UNSUPPORTED_PAGE: (
                False,
                MonitorStatus.INVALID_TARGET,
                None,
                "INVALID_TARGET",
            ),
            PluginErrorCode.AUTH_REQUIRED: (
                True,
                MonitorStatus.AUTH_REQUIRED,
                AUTH_RETRY_SECONDS,
                "AUTH_REQUIRED",
            ),
            PluginErrorCode.AUTH_FAILED: (
                True,
                MonitorStatus.AUTH_REQUIRED,
                AUTH_RETRY_SECONDS,
                "AUTH_FAILED",
            ),
            PluginErrorCode.PLUGIN_PARSE_ERROR: (
                True,
                MonitorStatus.PLUGIN_PARSE_ERROR,
                PARSER_RETRY_SECONDS,
                "PLUGIN_PARSE_ERROR",
            ),
            PluginErrorCode.RATE_LIMITED: (True, MonitorStatus.ERROR, None, "TRACKER_RATE_LIMITED"),
            PluginErrorCode.TEMPORARY_NETWORK_ERROR: (
                True,
                MonitorStatus.ERROR,
                None,
                "TRACKER_NETWORK_ERROR",
            ),
            PluginErrorCode.TRACKER_UNAVAILABLE: (
                True,
                MonitorStatus.ERROR,
                None,
                "TRACKER_UNAVAILABLE",
            ),
            PluginErrorCode.PROXY_ERROR: (
                True,
                MonitorStatus.PROXY_ERROR,
                None,
                "PROXY_ERROR",
            ),
        }
        retryable, status, delay, event_code = mapping[error.code]
        return JobExecutionFailure(
            str(error),
            retryable=retryable,
            monitor_status=status,
            retry_delay_seconds=delay,
            event_code=event_code,
        )
