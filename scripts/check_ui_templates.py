"""Small offline smoke check for the server-rendered UI.

Run: python scripts/check_ui_templates.py
No database, tracker session or network access is needed.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "torrwatch" / "web" / "templates"
STATIC = ROOT / "torrwatch" / "web" / "static"


def check() -> None:
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    # These are application-defined FastAPI template filters. Here we only parse.
    environment.filters["pretty_datetime"] = lambda value, timezone=None: value
    environment.filters["filesize"] = lambda value: str(value)

    paths = sorted(TEMPLATES.glob("*.html"))
    if not paths:
        raise RuntimeError("No HTML templates were found")
    for path in paths:
        environment.get_template(path.name)
        print(f"Jinja OK: {path.name}")

    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    monitors = (TEMPLATES / "monitors.html").read_text(encoding="utf-8")
    login = (TEMPLATES / "login.html").read_text(encoding="utf-8")
    expected = (
        "admin.css", "admin.js", "tw-redesign.css", "tw-redesign.js", "tw-unified.css",
    )
    for name in expected:
        path = STATIC / name
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"Missing/empty static file: {path}")
        if name in ("tw-redesign.css", "tw-redesign.js", "tw-unified.css"):
            if name not in base:
                raise RuntimeError(f"{name} is not loaded in base.html")
    if "tw-unified.css" not in login:
        raise RuntimeError("Login UI has no unified CSS")
    for route in (
        "/torrents/new", "/torrents/{{ m.id }}/check",
        "/torrents/{{ m.id }}/pause", "/torrents/{{ m.id }}/delete",
    ):
        if route not in monitors:
            raise RuntimeError(f"Required monitor action absent: {route}")
    if 'name="csrf"' not in monitors or 'name="csrf"' not in base:
        raise RuntimeError("Required CSRF field missing")
    common = {
        "username": "smoke-user",
        "csrf_token": "smoke-token",
        "timezone": "Europe/Moscow",
        "flash": None,
        "global_health": {"worker_ok": True, "attention": 0, "pending": 0},
    }
    monitor = {
        "id": 42,
        "name": "Test release",
        "plugin_id": "testtracker",
        "external_tracker_id": "11",
        "enabled": True,
        "paused": False,
        "status": "HEALTHY",
        "consecutive_failures": 0,
        "check_interval_seconds": 1800,
        "last_check_at": "2026-10-16T07:30:00Z",
        "next_check_at": None,
        "release_size": 500,
        "release_name": "Test release v2",
        "release_detected_at": "2026-10-16T07:30:00Z",
        "delivery_status": "SUCCESS",
        "canonical_url": "https://example.org/topic/11",
        "client_name": "qBittorrent",
    }
    rendered_monitors = environment.get_template("monitors.html").render(
        **common, path="/torrents", monitors=[monitor],
        selected_monitor=monitor, selected_timeline=[],
        selected_versions=[{
            "id": 2, "name": "Test release v2", "size": 500,
            "detected_at": "2026-10-16T07:30:00Z",
        }],
        active_monitors=1, paused_monitors=0, tracker_count=1,
    )
    for expected_text in ("Test release", "/torrents/42/check", "/torrents/42/pause"):
        if expected_text not in rendered_monitors:
            raise RuntimeError(f"Missing live monitor HTML: {expected_text}")
    empty_monitors = environment.get_template("monitors.html").render(
        **common, path="/torrents", monitors=[], selected_monitor=None,
        selected_timeline=[], selected_versions=[], active_monitors=0,
        paused_monitors=0, tracker_count=0,
    )
    if "Добавьте первый монитор" not in empty_monitors:
        raise RuntimeError("Empty monitor state failed to render")
    dashboard = {
        "total_monitors": 1, "active_monitors": 1, "error_monitors": 0,
        "updates_24h": 0, "pending_deliveries": 0, "pending_checks": 0,
        "pending_notifications": 0, "worker_heartbeat": None,
        "next_check": None,
    }
    rendered_dashboard = environment.get_template("dashboard.html").render(
        **common, path="/dashboard", dashboard=dashboard, events=[],
    )
    if "Фоновые операции" not in rendered_dashboard:
        raise RuntimeError("Dashboard failed to render")
    rendered_login = environment.get_template("login.html").render(
        csrf_token="smoke-token", error=None,
    )
    if 'action="/login"' not in rendered_login:
        raise RuntimeError("Sign-in action missing")
    print("Render OK: selected/empty monitors, dashboard and login")
    print(f"OK: {len(paths)} templates, {len(expected)} static assets")


if __name__ == "__main__":
    check()
