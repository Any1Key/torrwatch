"""Real Chromium screenshots & interactions against an isolated running TorrWatch.

Run after installing Playwright and its Chromium browser:
  python scripts/check_ui_browser.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from torrwatch.db.models import Event, MonitorItem, ReleaseVersion

BASE = "http://127.0.0.1:8765"
USER = "ui-browser-admin"
PASSWORD = "browser-test-password-not-for-production"


def wait_ready(process: subprocess.Popen, seconds: int = 70) -> None:
    for _ in range(seconds * 5):
        if process.poll() is not None:
            raise RuntimeError(f"Web process exited: {process.returncode}")
        try:
            with urllib.request.urlopen(BASE + "/health/ready", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("Timed out waiting for isolated web process")


def seed_test_data(data_dir: Path) -> None:
    engine = create_engine("sqlite:///" + str(data_dir / "torrwatch.db"))
    with Session(engine) as session:
        for index, (name, plugin, status, paused) in enumerate([
            ("TorrWatch 1080p demo", "rutracker", "HEALTHY", False),
            ("Documentary season", "nnmclub", "HEALTHY", False),
            ("Paused series", "kinozal", "HEALTHY", True),
            ("Needs fresh session", "rutracker", "AUTH_REQUIRED", False),
        ], 1):
            item = MonitorItem(
                name=name,
                original_url=f"https://example.invalid/topic/{index}",
                canonical_url=f"https://example.invalid/topic/{index}",
                external_tracker_id=str(index),
                plugin_id=plugin,
                enabled=True,
                paused=paused,
                current_status=status,
                check_interval_seconds=1800,
            )
            session.add(item)
            session.flush()
            release = ReleaseVersion(
                monitor_id=item.id, torrent_name=name,
                total_size=index * 1024 * 1024 * 1024,
                file_count=index + 2,
                version_key=f"ci-browser-{index}",
            )
            session.add(release)
            session.flush()
            item.current_release_id = release.id
            session.add(
                Event(
                    monitor_id=item.id, tracker_plugin_id=plugin,
                    level="INFO", event_code="SMOKE_SAMPLE",
                    message=f"Example event for {name}",
                )
            )
        session.commit()
    engine.dispose()


def test_browser(artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1536, "height": 1024}, device_scale_factor=1)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE + "/login")
        assert page.title() == "Вход — TorrWatch", page.title()
        assert page.locator("body.tw-login").count() == 1
        page.screenshot(path=str(artifact_dir / "login-1536.png"), full_page=True)
        page.locator("#username").fill(USER)
        page.locator("#password").fill(PASSWORD)
        page.get_by_role("button", name="Войти").click()
        page.wait_for_url("**/dashboard", timeout=15000)
        page.locator(".tw-overview-metrics").wait_for()
        page.screenshot(path=str(artifact_dir / "dashboard-1536.png"), full_page=True)
        assert page.locator(".tw-overview-metric").count() == 4
        print("BROWSER OK: login and live dashboard", flush=True)

        url = BASE + "/torrents"
        page.goto(url)
        page.locator("[data-tw-monitor]").first.wait_for()
        assert page.locator("[data-tw-monitor]").count() == 4
        assert page.locator("[data-tw-panel='overview']").is_visible()
        assert page.locator(".tw-detail").is_visible()
        page.screenshot(path=str(artifact_dir / "monitors-1536.png"), full_page=True)
        geometry = page.evaluate("""() => ({
          sidebar: document.querySelector('.sidebar').getBoundingClientRect().width,
          center: document.querySelector('.tw-center').getBoundingClientRect().width,
          detail: document.querySelector('.tw-detail').getBoundingClientRect().width,
          css: getComputedStyle(document.body).backgroundColor
        })""")
        print("BROWSER GEOMETRY:", geometry, flush=True)
        assert geometry["sidebar"] >= 220
        assert geometry["detail"] >= 320
        assert geometry["center"] > 360

        page.locator("[data-tw-search]").fill("Documentary")
        assert page.locator("[data-tw-monitor]:visible").count() == 1
        page.locator("[data-tw-search]").fill("")
        page.locator("[data-tw-filter='paused']").click()
        assert page.locator("[data-tw-monitor]:visible").count() == 1
        page.locator("[data-tw-filter='all']").click()
        assert page.locator("[data-tw-monitor]:visible").count() == 4
        page.locator("[data-tw-tab='versions']").click()
        assert page.locator("[data-tw-panel='versions']").is_visible()
        assert not page.locator("[data-tw-panel='overview']").is_visible()
        page.locator("[data-tw-tab='events']").click()
        assert page.locator("[data-tw-panel='events']").is_visible()
        page.locator("[data-tw-tab='overview']").click()
        print("BROWSER OK: monitor search, status filter, tabs", flush=True)

        pages = [
            "/integrations", "/clients", "/notifications", "/settings/sessions",
            "/queues", "/events", "/system", "/logs", "/help",
            "/torrents/new", "/proxies", "/paths",
            "/configure/clients", "/configure/sessions",
        ]
        for suffix in pages:
            resp = page.goto(BASE + suffix)
            assert resp and resp.status == 200, f"{suffix}: {resp.status if resp else 'no response'}"
            assert page.locator("body.tw-app").count() == 1, suffix
            assert page.locator(".sidebar").is_visible(), suffix
            assert page.locator("h1").first.is_visible(), suffix
            print("BROWSER OK:", suffix, flush=True)
        page.screenshot(path=str(artifact_dir / "settings-1536.png"), full_page=True)

        page.goto(url)
        for width, height in [(1366, 768), (820, 1180), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            page.reload()
            page.locator("[data-tw-monitor]").first.wait_for()
            dimensions = page.evaluate("""() => ({
                client:document.documentElement.clientWidth,
                scroll:document.documentElement.scrollWidth,
                detail:document.querySelector('.tw-detail').getBoundingClientRect().width
            })""")
            print(f"BROWSER responsive {width}x{height}: {dimensions}", flush=True)
            if dimensions["scroll"] - dimensions["client"] > 12:
                offenders = page.evaluate("""() => [...document.querySelectorAll('*')]
                  .map(el => ({tag:el.tagName.toLowerCase(),cls:typeof el.className==='string'?el.className.slice(0,90):'', text:(el.textContent||'').trim().slice(0,36), left:Math.round(el.getBoundingClientRect().left), right:Math.round(el.getBoundingClientRect().right), width:Math.round(el.getBoundingClientRect().width)}))
                  .filter(v=>v.right>innerWidth+12).sort((a,b)=>b.right-a.right).slice(0,16)""")
                print("BROWSER OVERFLOW ELEMENTS:", offenders, flush=True)
            assert dimensions["scroll"] - dimensions["client"] <= 12, f"Horizontal page overflow at {width}"
            assert dimensions["detail"] > 280, f"Detail panel too narrow at {width}"
            if width == 390:
                assert not page.locator("#sidebar").is_visible(), "Mobile sidebar visible before menu opened"
                page.locator("[data-menu-toggle]").click()
                assert page.locator("#sidebar").is_visible(), "Mobile menu failed to open"
                page.screenshot(path=str(artifact_dir / "monitors-390-menu-open.png"), full_page=True)
                page.locator("[data-menu-toggle]").click()
                assert not page.locator("#sidebar").is_visible(), "Mobile menu failed to close"
            page.screenshot(path=str(artifact_dir / f"monitors-{width}.png"), full_page=True)

        # All top-level screens must also remain usable on a phone.
        for suffix in ("/dashboard", "/integrations", "/clients", "/events", "/system", "/help", "/torrents/new"):
            page.goto(BASE + suffix)
            assert not page.locator("#sidebar").is_visible(), suffix + ": menu unexpectedly open"
            widths = page.evaluate("""() => ({
                viewport: document.documentElement.clientWidth,
                content: document.documentElement.scrollWidth
            })""")
            assert widths["content"] - widths["viewport"] <= 12, f"Mobile overflow on {suffix}: {widths}"
            print("BROWSER MOBILE OK:", suffix, flush=True)

        page.set_viewport_size({"width": 1536, "height": 1024})
        page.goto(BASE + "/dashboard")
        page.locator("[data-theme-toggle]").click()
        assert page.locator("body.dark-theme").count() == 0
        page.screenshot(path=str(artifact_dir / "dashboard-light-1536.png"), full_page=True)
        page.goto(BASE + "/torrents")
        light_detail = page.locator(".tw-detail").evaluate("(el) => getComputedStyle(el).backgroundColor")
        assert light_detail == "rgb(255, 255, 255)", f"Light monitor panel has dark styling: {light_detail}"
        page.screenshot(path=str(artifact_dir / "monitors-light-1536.png"), full_page=True)
        page.goto(BASE + "/dashboard")
        page.locator("[data-theme-toggle]").click()
        assert page.locator("body.dark-theme").count() == 1
        print("BROWSER OK: theme toggle", flush=True)

        if errors:
            raise AssertionError("Browser JavaScript errors: " + " | ".join(errors[:10]))
        browser.close()
        print("SUCCESS: Chromium pages, responsive layout and interactions", flush=True)


def main() -> None:
    artifact_dir = Path(os.environ.get("TORRWATCH_UI_ARTIFACT_DIR", "ui-browser-artifacts")).resolve()
    with tempfile.TemporaryDirectory(prefix="tw-live-ci-") as temp_dir:
        directory = Path(temp_dir)
        env = os.environ.copy()
        env.update({
            "TORRWATCH_DATA_DIR": str(directory),
            "TORRWATCH_ADMIN_USERNAME": USER,
            "TORRWATCH_ADMIN_PASSWORD": PASSWORD,
            "TORRWATCH_BIND": "127.0.0.1",
            "TORRWATCH_PORT": "8765",
            "TORRWATCH_LOG_LEVEL": "ERROR",
        })
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "torrwatch.main:create_app",
             "--factory", "--host", "127.0.0.1", "--port", "8765"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        try:
            wait_ready(process)
            seed_test_data(directory)
            test_browser(artifact_dir)
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
