"""Programmatic Alembic migration entrypoint used at process startup."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command
from torrwatch.core.config import Settings


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def alembic_config(settings: Settings) -> Config:
    root = project_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url)
    return config


def upgrade_database(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")
