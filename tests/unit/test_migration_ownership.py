from __future__ import annotations

from fastapi.testclient import TestClient

from torrwatch import cli, main
from torrwatch.worker import main as worker_main


def test_only_web_lifecycle_owns_automatic_migrations(
    settings: object, monkeypatch: object
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(main, "upgrade_database", lambda value: calls.append(value))  # type: ignore[union-attr]
    with TestClient(main.create_app(settings)):  # type: ignore[arg-type]
        pass
    assert calls == [settings]
    assert not hasattr(worker_main, "upgrade_database")
    assert not hasattr(cli, "upgrade_database")
