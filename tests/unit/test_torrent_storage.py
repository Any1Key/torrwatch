from __future__ import annotations

import stat
from pathlib import Path

import pytest

from torrwatch.torrent import TorrentStorageError, TorrentStore
from torrwatch.torrent import storage as storage_module

FIXTURE = Path(__file__).parents[1] / "fixtures" / "torrents" / "valid-v1.torrent"


def _fixture(path: Path) -> bytes:
    return path.read_bytes().removesuffix(b"\n")


def test_store_writes_private_validated_artifact_atomically(tmp_path: Path) -> None:
    store = TorrentStore(tmp_path / "torrents")
    payload = _fixture(FIXTURE)

    stored = store.store(7, 11, payload)

    assert stored.path == tmp_path / "torrents" / "7" / "11.torrent"
    assert stored.path.read_bytes() == payload
    assert stored.metadata.infohash_v1 == "ab843f6ded4224806b0880343b31cc4846a07788"
    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.path.parent.stat().st_mode) == 0o700


def test_failed_update_does_not_remove_existing_valid_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TorrentStore(tmp_path / "torrents")
    payload = _fixture(FIXTURE)
    current = store.store(7, 11, payload)

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)
    with pytest.raises(TorrentStorageError):
        store.store(7, 12, payload)

    assert current.path.read_bytes() == payload
    assert not (current.path.parent / "12.torrent").exists()
    assert not list(current.path.parent.glob("*.tmp"))


def test_existing_release_id_is_immutable(tmp_path: Path) -> None:
    store = TorrentStore(tmp_path / "torrents")
    v1_payload = _fixture(FIXTURE)
    v2_payload = _fixture(FIXTURE.parent / "valid-v2.torrent")
    stored = store.store(7, 11, v1_payload)

    with pytest.raises(TorrentStorageError, match="overwrite"):
        store.store(7, 11, v2_payload)

    assert stored.path.read_bytes() == v1_payload


def test_retention_never_removes_current_or_explicitly_protected_release(tmp_path: Path) -> None:
    store = TorrentStore(tmp_path / "torrents", retention_count=3)
    payload = _fixture(FIXTURE)
    for release_id in range(1, 7):
        store.store(7, release_id, payload)

    deleted = store.prune(7, [6, 5, 4, 3, 2, 1], protected_release_ids={1})

    assert [path.name for path in deleted] == ["3.torrent", "2.torrent"]
    assert {path.name for path in (tmp_path / "torrents" / "7").glob("*.torrent")} == {
        "1.torrent",
        "4.torrent",
        "5.torrent",
        "6.torrent",
    }
