"""Isolated FastAPI integration smoke — no network calls to trackers or clients.

Run with project dependencies installed:
    python scripts/check_ui_integration.py
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from torrwatch.core.config import Settings
from torrwatch.db.models import Event, MonitorItem, ReleaseVersion
from torrwatch.main import create_app


ROUTES = (
    "/dashboard", "/torrents", "/integrations", "/clients", "/notifications",
    "/settings/sessions", "/queues", "/events", "/system", "/logs", "/help",
    "/torrents/new", "/proxies", "/paths", "/configure/clients", "/configure/sessions",
    "/configure/proxies", "/configure/notifications", "/configure/paths",
)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    if match is None:
        raise AssertionError("Missing CSRF token in form")
    return match.group(1)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="torrwatch-ui-test-") as temporary:
        app = create_app(
            Settings(
                data_dir=Path(temporary),
                admin_username="ui-smoke-admin",
                admin_password=SecretStr("test-only-never-use-this-in-production"),
            )
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/health/ready")
            assert resp.status_code == 200, resp.text
            public = client.get("/login")
            assert public.status_code == 200, public.text
            csrf = csrf_from(public.text)
            assert "/static/tw-unified.css" in public.text
            blocked = client.get("/torrents", follow_redirects=False)
            assert blocked.status_code == 401, blocked.text
            failed_login = client.post(
                "/login", data={"username": "ui-smoke-admin", "password": "wrong", "csrf": csrf},
            )
            assert failed_login.status_code == 401, failed_login.text
            logged_in = client.post(
                "/login",
                data={
                    "username": "ui-smoke-admin",
                    "password": "test-only-never-use-this-in-production",
                    "csrf": csrf,
                },
                follow_redirects=False,
            )
            assert logged_in.status_code == 303, logged_in.text
            print("OK: auth, failed password, CSRF, health check")
            for url in ROUTES:
                result = client.get(url)
                assert result.status_code == 200, f"{url}: {result.status_code} {result.text[:700]}"
                assert "/static/tw-unified.css" in result.text, f"Missing common CSS on {url}"
                if url == "/torrents":
                    assert "Добавьте первый монитор" in result.text, result.text[:500]
                print("OK: GET", url)

            with app.state.database.session() as session:
                item = MonitorItem(
                    name="TorrWatch CI sample release",
                    original_url="https://example.invalid/viewtopic.php?t=1234",
                    canonical_url="https://example.invalid/viewtopic.php?t=1234",
                    external_tracker_id="1234",
                    plugin_id="rutracker",
                    enabled=True,
                    paused=False,
                    check_interval_seconds=1800,
                )
                session.add(item)
                session.flush()
                release = ReleaseVersion(
                    monitor_id=item.id,
                    torrent_name="TorrWatch CI sample release — 1080p",
                    total_size=512 * 1024 * 1024,
                    file_count=5,
                    version_key="ci-test",
                )
                session.add(release)
                session.flush()
                item.current_release_id = release.id
                session.add(
                    Event(
                        monitor_id=item.id,
                        tracker_plugin_id="rutracker",
                        level="INFO",
                        event_code="CI_TEST_EVENT",
                        message="Synthetic test event",
                    )
                )
                ident = item.id
            list_page = client.get("/torrents")
            assert list_page.status_code == 200, list_page.text[:900]
            for expected in ("TorrWatch CI sample release", f"/torrents/{ident}/check", "Версии"):
                assert expected in list_page.text, f"Missing '{expected}'"
            detail = client.get(f"/torrents/{ident}")
            assert detail.status_code == 200, detail.text[:900]
            selected = client.get(f"/torrents?selected={ident}")
            assert selected.status_code == 200 and "Synthetic test event" in selected.text
            assert "512.0 МБ" in selected.text
            events = client.get("/events")
            assert events.status_code == 200 and "Synthetic test event" in events.text
            print("OK: persisted monitor + release + event rendered in browser routes")

            no_csrf = client.post(
                f"/torrents/{ident}/pause", data={}, follow_redirects=False
            )
            assert no_csrf.status_code == 403, f"CSRF enforcement failed: {no_csrf.status_code}"
            paused = client.post(
                f"/torrents/{ident}/pause",
                data={"csrf": csrf},
                follow_redirects=False,
            )
            assert paused.status_code == 303, paused.text[:500]
            with app.state.database.session() as session:
                assert session.get(MonitorItem, ident).paused
            resumed = client.post(
                f"/torrents/{ident}/pause",
                data={"csrf": csrf},
                follow_redirects=False,
            )
            assert resumed.status_code == 303, resumed.text[:500]
            with app.state.database.session() as session:
                assert not session.get(MonitorItem, ident).paused
            print("OK: pause/resume action and CSRF rejection")

            for path in (f"/torrents/{ident}/edit", f"/torrents/{ident}/delete"):
                result = client.get(path)
                assert result.status_code == 200, (path, result.status_code, result.text[:500])
                print("OK: GET", path)
            checked = client.post(
                f"/torrents/{ident}/check",
                data={"csrf": csrf},
                headers={"accept": "application/json"},
            )
            assert checked.status_code == 202, (checked.status_code, checked.text[:700])
            print("OK: enqueue check without performing an outbound tracker request")
        print("SUCCESS: isolated TorrWatch integration UI smoke")


if __name__ == "__main__":
    main()
