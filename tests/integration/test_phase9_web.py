"""Offline coverage for Phase 9 browser orchestration."""

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


def test_phase9_dashboard_pages_require_administrator(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert 'name="password"' in client.get("/").text
    _login(client)
    assert "Торренты" in client.get("/").text
    for page in (
        "/torrents",
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
    tracker_page = client.get("/trackers").text
    assert all(plugin in tracker_page for plugin in ("rutracker", "nnmclub", "kinozal"))


def test_phase9_browser_form_has_csrf_and_safe_validation_error(client: TestClient) -> None:
    token = _login(client)
    rejected = client.post(
        "/configure/monitors",
        data={
            "name": " ",
            "url": "https://rutracker.org/forum/viewtopic.php?t=12345",
            "interval_seconds": "1",
            "csrf": token,
        },
    )
    assert rejected.status_code == 422
    assert "Минимальный интервал" in rejected.text
    assert client.post("/torrents/999/check", data={"csrf": "wrong"}).status_code == 403
