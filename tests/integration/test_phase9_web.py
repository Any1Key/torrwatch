"""Offline coverage for Phase 9 browser/API orchestration."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def _csrf(response_text: str) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert matched is not None
    return matched.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "a-strong-bootstrap-password", "csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/").text)


def test_phase9_dashboard_pages_and_read_api_require_administrator(client: TestClient) -> None:
    assert client.get("/").status_code == 401
    _login(client)
    assert "Мониторы" in client.get("/").text
    for page in (
        "/monitors",
        "/trackers",
        "/tracker-accounts",
        "/proxies",
        "/clients",
        "/notifications",
        "/events",
        "/settings",
        "/system",
    ):
        assert client.get(page).status_code == 200
    plugins = client.get("/api/v1/trackers")
    assert plugins.status_code == 200
    assert {item["id"] for item in plugins.json()} >= {"rutracker", "nnmclub", "kinozal"}
    assert client.get("/api/v1/system").json()["dashboard"]["total_monitors"] == 0


def test_phase9_monitor_create_validation_and_durable_manual_check(client: TestClient) -> None:
    token = _login(client)
    invalid = client.post(
        "/api/v1/monitors",
        json={"name": "bad", "url": "https://example.invalid/"},
        headers={"X-CSRF-Token": token},
    )
    assert invalid.status_code == 422
    denied = client.post(
        "/api/v1/monitors",
        json={"name": "release", "url": "https://rutracker.org/forum/viewtopic.php?t=12345"},
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/v1/monitors",
        json={"name": "release", "url": "https://rutracker.org/forum/viewtopic.php?t=12345"},
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 201
    monitor = created.json()
    assert monitor["plugin_id"] == "rutracker"
    assert monitor["external_tracker_id"] == "12345"
    assert "12345" in monitor["canonical_url"]
    monitor_id = monitor["id"]
    queued = client.post(f"/api/v1/monitors/{monitor_id}/check", headers={"X-CSRF-Token": token})
    assert queued.status_code == 202
    assert queued.json()["queued"] is True
    duplicate = client.post(f"/api/v1/monitors/{monitor_id}/check", headers={"X-CSRF-Token": token})
    assert duplicate.json()["queued"] is False
    assert client.get(f"/monitors/{monitor_id}").status_code == 200
    timeline = client.get(f"/api/v1/monitors/{monitor_id}/events").json()
    assert timeline[0]["code"] == "CHECK_SCHEDULED"


def test_phase9_browser_form_has_csrf_and_safe_validation_error(client: TestClient) -> None:
    token = _login(client)
    rejected = client.post(
        "/monitors",
        data={
            "name": " ",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "interval_seconds": "1",
            "csrf": token,
        },
    )
    assert rejected.status_code == 422
    assert "Недопустимые" in rejected.text or "Укажите имя" in rejected.text
    assert client.post("/monitors/999/check", data={"csrf": "wrong"}).status_code == 403
