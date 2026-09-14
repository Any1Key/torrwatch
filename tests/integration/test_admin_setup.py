"""Offline browser setup regression: write-only secrets and enqueue-only actions."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_phase9_web import _login
from torrwatch.core.secrets import SecretBox
from torrwatch.db.models import (
    MonitorItem,
    NotificationChannel,
    NotificationJob,
    StoragePath,
    TorrentClient,
    TrackerSession,
)


@pytest.mark.parametrize(
    "resource", ["clients", "sessions", "proxies", "notifications", "paths", "monitors"]
)
def test_setup_forms_require_auth_csrf_and_explain_fields(
    client: TestClient, resource: str
) -> None:
    path = f"/configure/{resource}"
    assert client.get(path).status_code == 401
    _login(client)
    page = client.get(path)
    assert page.status_code == 200
    assert 'name="csrf"' in page.text
    assert "Сохранить" in page.text and "Отмена" in page.text
    assert client.post(path, data={"name": "safe"}).status_code == 403


def test_fresh_dashboard_empty_states_and_static_assets(client: TestClient) -> None:
    _login(client)
    home = client.get("/").text
    assert "Завершите первоначальную настройку" in home
    for path in (
        "/configure/clients",
        "/configure/sessions",
        "/monitors/new",
        "/configure/notifications",
        "/configure/paths",
    ):
        assert path in home
    for path in (
        "/clients",
        "/proxies",
        "/notifications",
        "/monitors",
        "/events",
        "/system",
        "/trackers",
    ):
        assert client.get(path).status_code == 200
    assert "Добавить" in client.get("/clients").text
    assert client.get("/static/admin.css").status_code == 200
    assert client.get("/static/admin.js").status_code == 200


def test_browser_complete_setup_and_durable_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbid_network(*args, **kwargs):
        pytest.fail("Browser actions must not perform outbound HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbid_network)
    token = _login(client)

    def save(resource: str, **values: str):
        response = client.post(
            f"/configure/{resource}", data={"csrf": token, "enabled": "on", **values}
        )
        assert response.status_code == 200
        return response

    secret = "offline-private-value"
    save(
        "clients",
        name="Lab client",
        type="TRANSMISSION",
        url="http://192.168.1.2:9091",
        username="lab",
        password=secret,
    )
    save(
        "proxies",
        name="Lab proxy",
        type="SOCKS5",
        host="proxy.example",
        port="1080",
        username="lab",
        password=secret,
    )
    save("sessions", plugin="rutracker", cookie=f"session={secret}", user_agent="OfflineBrowser")
    resolved = client.get(
        "/resolve-url", params={"url": "https://rutracker.org/forum/viewtopic.php?t=12345"}
    )
    assert resolved.status_code == 200 and resolved.json()["id"] == "12345"
    values = dict(
        name="Lab release",
        url=resolved.json()["url"],
        interval_seconds="1800",
        client="1",
        proxy="1",
        session="shared",
    )
    page = save("monitors", **values)
    assert "Монитор сохранён" in page.text
    assert "12345" in page.text
    db = client.app.state.database
    with db.session() as s:
        monitor = s.get(MonitorItem, 1)
        assert (
            monitor.tracker_account_id,
            monitor.torrent_client_id,
            monitor.proxy_override_id,
        ) == (1, 1, 1)
        stored = s.scalar(select(TrackerSession))
        assert stored.namespace == "rutracker:account:1" and secret not in stored.encrypted_cookies
        saved_client = s.get(TorrentClient, 1)
        assert secret not in saved_client.encrypted_password
    for path in (
        "/configure/clients/1",
        "/configure/proxies/1",
        "/configure/sessions",
        "/settings/sessions",
        "/api/v1/clients",
    ):
        assert secret not in client.get(path).text
    assert (
        client.post(
            "/configure/clients/1",
            data={
                "csrf": token,
                "name": "Renamed client",
                "type": "TRANSMISSION",
                "url": "http://192.168.1.2:9091",
                "enabled": "on",
                "username": "lab",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/configure/proxies/1",
            data={
                "csrf": token,
                "name": "Renamed proxy",
                "type": "SOCKS5",
                "host": "proxy.example",
                "port": "1080",
                "username": "lab",
                "enabled": "on",
            },
        ).status_code
        == 200
    )
    with db.session() as s:
        box = SecretBox(client.app.state.settings.resolved_master_key_file)
        assert box.decrypt(s.get(TorrentClient, 1).encrypted_password) == secret
    first = client.post("/monitors/1/check", data={"csrf": token})
    assert "поставлена в очередь" in first.text
    assert "уже в очереди" in client.post("/monitors/1/check", data={"csrf": token}).text
    assert client.get("/api/v1/system").json()["pending_monitor_jobs"] == 1
    assert client.get("/monitors/1/edit").status_code == 200
    assert client.post("/monitors/1/pause", data={"csrf": token}).status_code == 200
    assert (
        client.post(
            "/configure/monitors/1",
            data={"csrf": token, "enabled": "on", **values, "session": "monitor"},
        ).status_code
        == 200
    )
    with db.session() as s:
        assert s.get(MonitorItem, 1).tracker_account_id is None


def test_named_storage_path_and_automatic_monitor_name(client: TestClient) -> None:
    token = _login(client)
    response = client.post(
        "/configure/paths",
        data={
            "csrf": token,
            "name": "Сериалы",
            "path": "/volume1/Download/complete/Serials",
            "enabled": "on",
        },
    )
    assert response.status_code == 200
    response = client.post(
        "/configure/monitors",
        data={
            "csrf": token,
            "name": "",
            "url": "https://rutracker.org/forum/viewtopic.php?t=98765",
            "interval_seconds": "1800",
            "client": "",
            "storage_path": "1",
            "session": "shared",
            "enabled": "on",
        },
    )
    assert response.status_code == 200
    with client.app.state.database.session() as session:
        monitor = session.query(MonitorItem).order_by(MonitorItem.id.desc()).first()
        path = session.get(StoragePath, 1)
        assert monitor is not None and monitor.name.startswith("RuTracker #")
        assert path is not None and monitor.client_save_path == path.path


@pytest.mark.parametrize("kind", ["TELEGRAM", "WEBHOOK"])
def test_notification_secret_edit_and_test_queue(client: TestClient, kind: str) -> None:
    token = _login(client)
    secret = "offline-notification-secret"
    data = {
        "csrf": token,
        "name": "Test channel",
        "type": kind,
        "enabled": "on",
        "events": "DELIVERY_SUCCESS",
        "bot_token": secret,
        "chat_id": "123",
        "url": "http://192.168.1.3/hook",
        "authorization": secret,
        "headers_json": json.dumps({"X-Key": secret}),
    }
    response = client.post("/configure/notifications", data=data)
    assert response.status_code == 200 and secret not in response.text
    edit = client.get("/configure/notifications/1").text
    assert secret not in edit and "DELIVERY_SUCCESS" in edit
    assert (
        client.post(
            "/configure/notifications/1",
            data={
                "csrf": token,
                "name": "Renamed",
                "type": kind,
                "enabled": "on",
                "events": "DELIVERY_SUCCESS",
            },
        ).status_code
        == 200
    )
    for _ in range(2):
        assert client.post("/notifications/1/test", data={"csrf": token}).status_code == 200
    with client.app.state.database.session() as s:
        channel = s.get(NotificationChannel, 1)
        assert secret not in channel.encrypted_config
        box = SecretBox(client.app.state.settings.resolved_master_key_file)
        assert secret in box.decrypt(channel.encrypted_config)
        assert len(list(s.scalars(select(NotificationJob)))) == 1


@pytest.mark.parametrize(
    "resource,data",
    [
        ("clients", {"type": "QBITTORRENT", "url": "file:///tmp/no", "password": "do-not-echo"}),
        ("proxies", {"type": "HTTP", "port": "0"}),
        ("notifications", {"type": "WEBHOOK", "url": "file:///no"}),
        ("sessions", {"plugin": "rutracker", "cookie": "do-not-echo"}),
        ("monitors", {"url": "https://example.invalid/", "interval_seconds": "1"}),
    ],
)
def test_validation_preserves_nonsecrets(
    client: TestClient, resource: str, data: dict[str, str]
) -> None:
    token = _login(client)
    response = client.post(
        f"/configure/{resource}", data={"csrf": token, "name": "Keep this label", **data}
    )
    assert response.status_code == 422
    assert "Не удалось сохранить" in response.text
    assert "do-not-echo" not in response.text
    if resource != "sessions":
        assert "Keep this label" in response.text


def test_unknown_resources_and_actions(client: TestClient) -> None:
    token = _login(client)
    for path in (
        "/settings/unknown",
        "/configure/unknown",
        "/monitors/999",
        "/monitors/999/edit",
        "/configure/clients/999",
    ):
        assert client.get(path).status_code == 404
    assert client.get("/resolve-url", params={"url": "https://example.invalid"}).status_code == 422
    assert client.post("/monitors/999/check", data={"csrf": token}).status_code == 404
    assert client.post("/notifications/999/test", data={"csrf": token}).status_code == 422
