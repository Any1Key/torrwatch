from __future__ import annotations

import re

from fastapi.testclient import TestClient


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert "torrwatch_phase0_info 1" in client.get("/metrics").text


def test_login_logout_and_csrf_protection(client: TestClient) -> None:
    login_page = client.get("/login")
    csrf = csrf_from(login_page.text)

    rejected = client.post("/login", data={"username": "admin", "password": "bad", "csrf": csrf})
    assert rejected.status_code == 401

    csrf = csrf_from(client.get("/login").text)
    logged_in = client.post(
        "/login",
        data={"username": "admin", "password": "a-strong-bootstrap-password", "csrf": csrf},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    home = client.get("/")
    assert home.status_code == 200
    assert "Вы вошли как admin" in home.text

    forbidden = client.post("/logout", data={"csrf": "not-the-session-token"})
    assert forbidden.status_code == 403
    csrf = csrf_from(client.get("/").text)
    assert client.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200
    assert 'name="password"' in client.get("/").text
