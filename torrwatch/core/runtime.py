"""Runtime directory and bootstrap-secret handling."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from torrwatch.core.config import Settings


def _write_private_file(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


def ensure_runtime_files(settings: Settings) -> None:
    """Create project-owned state directories and an external master-key placeholder."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    settings.resolved_plugins_dir.mkdir(exist_ok=True)
    os.chmod(settings.resolved_plugins_dir, 0o700)
    key_file = settings.resolved_master_key_file
    if not key_file.exists():
        _write_private_file(key_file, secrets.token_bytes(32))
    os.chmod(key_file, 0o600)
