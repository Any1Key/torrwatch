from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from torrwatch.core.config import Settings
from torrwatch.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        admin_username="admin",
        admin_password="a-strong-bootstrap-password",
        worker_heartbeat_seconds=5,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
