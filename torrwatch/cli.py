"""Safe operational CLI commands required for the final v1 release."""

from __future__ import annotations

import argparse
import getpass
import shutil
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import select

from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.security import hash_password
from torrwatch.db.database import Database
from torrwatch.db.models import User, WorkerHeartbeat
from torrwatch.services.jobs import JobRepository, now_utc
from torrwatch.trackers.loader import load_plugin_registry


def _database(settings: Settings) -> Database:
    ensure_runtime_files(settings)
    return Database(settings.resolved_database_url)


def doctor(settings: Settings) -> int:
    """Print only safe PASS/WARN diagnostics and return nonzero for failures."""
    try:
        ensure_runtime_files(settings)
        key = settings.resolved_master_key_file
        db = _database(settings)
        try:
            with db.session() as session:
                session.execute(select(1))
                heartbeat = session.scalar(
                    select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1)
                )
            plugins = load_plugin_registry(settings).manifests()
        finally:
            db.dispose()
    except Exception as error:
        print(f"FAIL configuration/runtime: {type(error).__name__}")
        return 1
    print("PASS configuration, master key, storage and SQLite")
    print(f"PASS plugin registry: {len(plugins)} built-in plugin(s)")
    print("PASS worker heartbeat" if heartbeat else "WARN worker heartbeat unavailable")
    print(f"PASS master key permissions: {key.stat().st_mode & 0o777:03o}")
    return 0


def backup(destination: Path, include_master_key: bool, settings: Settings) -> int:
    """Create a consistent SQLite backup plus private torrent history."""
    ensure_runtime_files(settings)
    if destination.exists():
        raise ValueError("Backup destination must not already exist.")
    destination.mkdir(mode=0o700, parents=True)
    source = sqlite3.connect(settings.resolved_database_url.removeprefix("sqlite:///"))
    target = sqlite3.connect(destination / "torrwatch.db")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    if settings.resolved_torrents_dir.exists():
        shutil.copytree(
            settings.resolved_torrents_dir, destination / "torrents", copy_function=shutil.copy2
        )
    if include_master_key:
        shutil.copy2(settings.resolved_master_key_file, destination / "master.key")
    print(
        "PASS backup created"
        + (" with explicitly requested master key" if include_master_key else " without master key")
    )
    return 0


def plugins_list(settings: Settings) -> int:
    registry = load_plugin_registry(settings)
    for item in registry.manifests():
        print(f"{item.id}\t{item.version}\t{','.join(item.domains)}")
    return 0


def enqueue_check(monitor_id: int, settings: Settings) -> int:
    db = _database(settings)
    try:
        if not JobRepository(db).enqueue_monitor_check(monitor_id, now_utc()):
            print("WARN check already queued or monitor unavailable")
            return 1
    finally:
        db.dispose()
    print("PASS monitor check queued")
    return 0


def reset_password(settings: Settings) -> int:
    first = getpass.getpass("New administrator password: ")
    second = getpass.getpass("Repeat administrator password: ")
    if first != second or len(first) < 12:
        print("FAIL password does not match or is shorter than 12 characters")
        return 1
    db = _database(settings)
    try:
        with db.session() as session:
            user = session.scalar(select(User).limit(1))
            if user is None:
                print("FAIL administrator does not exist")
                return 1
            user.password_hash = hash_password(first)
    finally:
        db.dispose()
    print("PASS administrator password reset")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="torrwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    backup_parser.add_argument("--include-master-key", action="store_true")
    plugins = sub.add_parser("plugins").add_subparsers(dest="plugins_command", required=True)
    plugins.add_parser("list")
    check = sub.add_parser("check")
    check.add_argument("monitor_id", type=int)
    admin = sub.add_parser("admin").add_subparsers(dest="admin_command", required=True)
    admin.add_parser("reset-password")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "doctor":
        return doctor(settings)
    if args.command == "backup":
        return backup(args.destination, args.include_master_key, settings)
    if args.command == "plugins":
        return plugins_list(settings)
    if args.command == "check":
        return enqueue_check(args.monitor_id, settings)
    return reset_password(settings)


if __name__ == "__main__":
    sys.exit(main())
