from __future__ import annotations

import sqlite3
from pathlib import Path

from torrwatch import cli
from torrwatch.cli import backup, doctor, enqueue_check, plugins_list
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.bootstrap import bootstrap_admin
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.services.jobs import MonitorRepository, now_utc


def prepared(settings: object) -> Database:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    bootstrap_admin(database, settings)  # type: ignore[arg-type]
    return database


def test_doctor_and_plugins_list_are_safe(settings: object, capsys: object) -> None:
    database = prepared(settings)
    database.dispose()
    assert doctor(settings) == 0  # type: ignore[arg-type]
    assert "PASS" in capsys.readouterr().out  # type: ignore[union-attr]
    assert plugins_list(settings) == 0  # type: ignore[arg-type]
    assert "rutracker" in capsys.readouterr().out  # type: ignore[union-attr]


def test_backup_is_consistent_and_excludes_master_key_by_default(
    settings: object, tmp_path: Path
) -> None:
    database = prepared(settings)
    database.dispose()
    destination = tmp_path / "backup"
    assert backup(destination, False, settings) == 0  # type: ignore[arg-type]
    assert (destination / "torrwatch.db").is_file()
    assert not (destination / "master.key").exists()
    with sqlite3.connect(destination / "torrwatch.db") as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
    keyed = tmp_path / "backup-with-key"
    assert backup(keyed, True, settings) == 0  # type: ignore[arg-type]
    assert (keyed / "master.key").stat().st_mode & 0o777 == 0o600


def test_check_enqueues_durable_job(settings: object) -> None:
    database = prepared(settings)
    try:
        monitor = MonitorRepository(database).create(
            name="cli",
            url="https://rutracker.org/forum/viewtopic.php?t=1",
            plugin_id="rutracker",
            next_check_at=now_utc(),
        )
    finally:
        database.dispose()
    assert enqueue_check(monitor.id, settings) == 0  # type: ignore[arg-type]


def test_cli_dispatch_and_password_validation(settings: object, monkeypatch: object) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)  # type: ignore[union-attr]
    monkeypatch.setattr(cli, "doctor", lambda _: 7)  # type: ignore[union-attr]
    monkeypatch.setattr(cli, "plugins_list", lambda _: 8)  # type: ignore[union-attr]
    assert cli.main(["doctor"]) == 7
    assert cli.main(["plugins", "list"]) == 8
    values = iter(("short", "short"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(values))  # type: ignore[union-attr]
    assert cli.reset_password(settings) == 1  # type: ignore[arg-type]
