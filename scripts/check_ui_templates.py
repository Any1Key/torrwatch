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
    print(f"OK: {len(paths)} templates, {len(expected)} static assets")


if __name__ == "__main__":
    check()
