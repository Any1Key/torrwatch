"""Use a browser for RuTracker topic pages challenged by Cloudflare."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select

from torrwatch.core.config import Settings
from torrwatch.db.database import Database
from torrwatch.db.models import SystemSetting
from torrwatch.services.flaresolverr import FlareSolverrClient, FlareSolverrError
from torrwatch.trackers.rutracker.parser import (
    RuTrackerAuthenticationRequired,
    RuTrackerParseError,
    parse_topic_page,
)
from torrwatch.transport.cookies import SessionStore
from torrwatch.transport.http import TransportResponse


async def solve_rutracker_topic(
    url: str,
    response: TransportResponse,
    *,
    database: Database,
    settings: Settings,
    sessions: SessionStore,
    namespace: str,
) -> TransportResponse:
    parsed = urlsplit(url)
    topic = parse_qs(parsed.query).get("t", [])
    # Cloudflare does not always return a 403: depending on the edge node it
    # can serve the challenge shell with HTTP 200.  In that case the old
    # status-only check let the parser see the challenge HTML and the monitor
    # failed before the browser fallback had a chance to run.
    body = response.body.lower()
    challenged = (
        response.headers.get("cf-mitigated", "").lower() == "challenge"
        or b"/cdn-cgi/challenge-platform/" in body
        or b"cf-chl-" in body
        or b"just a moment..." in body
        or b"enable javascript and cookies to continue" in body
    )
    if (
        not challenged
        or parsed.hostname != "rutracker.org"
        or parsed.path != "/forum/viewtopic.php"
        or len(topic) != 1
        or not topic[0].isdigit()
    ):
        return response
    with database.session() as session:
        values = dict(
            session.execute(
                select(SystemSetting.key, SystemSetting.value).where(
                    SystemSetting.key.in_(("flaresolverr_enabled", "flaresolverr_url"))
                )
            ).all()
        )
    endpoint = values.get("flaresolverr_url") or settings.flaresolverr_url
    if values.get("flaresolverr_enabled", "true") == "false" or not endpoint:
        return response
    cookies, _ = sessions.load(namespace)
    cookie_data = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in cookies.jar
        if c.domain.lstrip(".") == "rutracker.org"
    ]
    client = FlareSolverrClient(endpoint)
    browser_session = f"torrwatch-topic-{uuid.uuid4().hex}"
    try:
        await asyncio.wait_for(client.create_session(browser_session), timeout=15)
        solution = await asyncio.wait_for(
            client.request(
                url=url, session=browser_session, cookies=cookie_data, max_timeout_ms=40000
            ),
            timeout=45,
        )
        if solution.status != 200:
            return response
        parse_topic_page(solution.html, url, topic[0])
        fresh = httpx.Cookies()
        for cookie in solution.cookies:
            domain = str(cookie.get("domain") or ".rutracker.org")
            if (
                domain.lstrip(".") == "rutracker.org"
                and isinstance(cookie.get("name"), str)
                and isinstance(cookie.get("value"), str)
            ):
                fresh.set(
                    cookie["name"],
                    cookie["value"],
                    domain=domain,
                    path=str(cookie.get("path") or "/"),
                )
        if not any(cookie.name == "bb_session" for cookie in fresh.jar):
            return response
        sessions.save(namespace, fresh, solution.user_agent)
        return TransportResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            solution.html.encode("utf-8"),
            url,
            response.elapsed_ms,
            response.attempts,
        )
    except (TimeoutError, FlareSolverrError, RuTrackerAuthenticationRequired, RuTrackerParseError):
        return response
    finally:
        with suppress(FlareSolverrError, TimeoutError):
            await asyncio.wait_for(client.destroy_session(browser_session), timeout=5)
