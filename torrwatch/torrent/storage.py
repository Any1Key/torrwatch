"""Atomic, private persistence for validated torrent artifacts."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from torrwatch.torrent.metainfo import TorrentMetadata, parse_metainfo


class TorrentStorageError(RuntimeError):
    """A validated torrent could not be persisted safely."""


@dataclass(frozen=True)
class StoredTorrent:
    """A torrent file safely committed to its monitor-specific namespace."""

    path: Path
    metadata: TorrentMetadata


class TorrentStore:
    """Stores immutable release artifacts without replacing a known-good file."""

    def __init__(self, root: Path, retention_count: int = 5) -> None:
        if retention_count < 1:
            raise ValueError("retention_count must be at least one.")
        self._root = root
        self._retention_count = retention_count

    @staticmethod
    def _release_name(release_id: int) -> str:
        if release_id < 1:
            raise ValueError("release_id must be positive.")
        return f"{release_id}.torrent"

    def _monitor_directory(self, monitor_id: int) -> Path:
        if monitor_id < 1:
            raise ValueError("monitor_id must be positive.")
        self._root.mkdir(parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)
        directory = self._root / str(monitor_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def store(self, monitor_id: int, release_id: int, payload: bytes) -> StoredTorrent:
        """Validate, fsync, validate again, then atomically publish an artifact.

        Release IDs are immutable: an existing path is returned only when the
        exact same artifact is supplied. This prevents a failed update from ever
        replacing a previously valid release.
        """

        metadata = parse_metainfo(payload)
        directory = self._monitor_directory(monitor_id)
        destination = directory / self._release_name(release_id)
        if destination.exists():
            existing = destination.read_bytes()
            existing_metadata = parse_metainfo(existing)
            if existing_metadata.torrent_sha256 != metadata.torrent_sha256:
                raise TorrentStorageError("Refusing to overwrite an existing release artifact.")
            return StoredTorrent(path=destination, metadata=existing_metadata)

        descriptor: int | None = None
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=directory, prefix=f".{release_id}.", suffix=".torrent.tmp"
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            persisted_metadata = parse_metainfo(temporary_path.read_bytes())
            if persisted_metadata.torrent_sha256 != metadata.torrent_sha256:
                raise TorrentStorageError("Persisted torrent hash differs from input.")
            os.replace(temporary_path, destination)
            self._fsync_directory(directory)
            return StoredTorrent(path=destination, metadata=persisted_metadata)
        except OSError as exc:
            raise TorrentStorageError("Unable to atomically store torrent artifact.") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()

    def prune(
        self,
        monitor_id: int,
        release_ids_newest_first: Sequence[int],
        protected_release_ids: Collection[int] = (),
    ) -> tuple[Path, ...]:
        """Remove only unprotected artifacts older than the retention window.

        Callers provide all releases that must be retained (at minimum the
        monitor's current release; future delivery jobs add their references).
        Files are deliberately unlinked only after their ID is outside the
        newest retention window, so an incomplete or duplicate history cannot
        remove a current artifact.
        """

        directory = self._monitor_directory(monitor_id)
        protected = set(protected_release_ids)
        retained = set(release_ids_newest_first[: self._retention_count]) | protected
        deleted: list[Path] = []
        for release_id in release_ids_newest_first[self._retention_count :]:
            if release_id in retained:
                continue
            path = directory / self._release_name(release_id)
            if path.exists():
                path.unlink()
                deleted.append(path)
        if deleted:
            self._fsync_directory(directory)
        return tuple(deleted)
