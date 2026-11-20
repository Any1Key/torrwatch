from __future__ import annotations

import stat

from torrwatch.core.config import Settings
from torrwatch.core.runtime import ensure_runtime_files


def test_settings_resolve_data_paths(tmp_path: object) -> None:
    settings = Settings(data_dir=tmp_path / "state")  # type: ignore[operator]

    assert settings.resolved_database_url == f"sqlite:///{tmp_path / 'state' / 'torrwatch.db'}"  # type: ignore[operator]
    assert settings.resolved_master_key_file == tmp_path / "state" / "master.key"  # type: ignore[operator]
    assert settings.resolved_torrents_dir == tmp_path / "state" / "torrents"  # type: ignore[operator]


def test_runtime_creates_private_master_key(settings: Settings) -> None:
    ensure_runtime_files(settings)

    key_file = settings.resolved_master_key_file
    assert key_file.exists()
    assert len(key_file.read_bytes()) == 32
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert settings.resolved_torrents_dir.is_dir()
    assert stat.S_IMODE(settings.resolved_torrents_dir.stat().st_mode) == 0o700
