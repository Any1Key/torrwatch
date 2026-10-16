"""Automatic tracker-session refresh through a private FlareSolverr service."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin

import httpx

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import TrackerSession
from torrwatch.transport.cookies import SessionStore

logger = logging.getLogger(__name__)


class FlareSolverrError(RuntimeError):
    """A session refresh could not be completed."""


@dataclass(frozen=True)
class SolverResult:
    cookies: list[dict[str, object]]
    user_agent: str
    html: str
    status: int


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and self._current is None:
            self._current = {"action": values.get("action") or "", "fields": {}}
        elif tag == "input" and self._current is not None:
            name = values.get("name")
            if name:
                fields = self._current["fields"]
                if isinstance(fields, dict):
                    fields[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None

    @property
    def action(self) -> str:
        form = self._login_form()
        return str(form.get("action") or "") if form else ""

    @property
    def fields(self) -> dict[str, str]:
        form = self._login_form()
        fields = form.get("fields") if form else {}
        return dict(fields) if isinstance(fields, dict) else {}

    def _login_form(self) -> dict[str, object] | None:
        for form in self.forms:
            fields = form.get("fields")
            if isinstance(fields, dict) and "password" in fields:
                return form
        return self.forms[0] if self.forms else None


class FlareSolverrClient:
    def __init__(self, endpoint: str, timeout_seconds: float = 150.0) -> None:
        self.endpoint = endpoint.rstrip("/") + "/v1"
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0)

    async def request(
        self,
        *,
        url: str,
        session: str,
        cookies: list[dict[str, object]] | None = None,
        post_data: str | None = None,
    ) -> SolverResult:
        payload: dict[str, object] = {
            "cmd": "request.post" if post_data is not None else "request.get",
            "url": url,
            "session": session,
            "session_ttl_minutes": 180,
            "maxTimeout": 120000,
            "returnOnlyCookies": False,
            "cookies": cookies or [],
            "waitInSeconds": 2,
        }
        if post_data is not None:
            payload["postData"] = post_data
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise FlareSolverrError("FlareSolverr is unavailable.") from error
        if body.get("status") != "ok" or not isinstance(body.get("solution"), dict):
            message = str(body.get("message") or "FlareSolverr returned an error.")
            raise FlareSolverrError(message)
        solution = body["solution"]
        cookies_value = solution.get("cookies")
        user_agent = solution.get("userAgent")
        html = solution.get("response")
        if not isinstance(cookies_value, list) or not isinstance(user_agent, str):
            raise FlareSolverrError("FlareSolverr returned an invalid session.")
        return SolverResult(
            cookies=[item for item in cookies_value if isinstance(item, dict)],
            user_agent=user_agent,
            html=html if isinstance(html, str) else "",
            status=int(solution.get("status") or 0),
        )

    async def destroy_session(self, session: str) -> None:
        """Drop a possibly stale browser session before a fresh login flow."""
        payload = {"cmd": "sessions.destroy", "session": session}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload)
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as error:
            raise FlareSolverrError("FlareSolverr is unavailable.") from error


def _cookie_payload(session: TrackerSession, secrets: SecretBox) -> list[dict[str, object]]:
    if not session.encrypted_cookies:
        return []
    value = json.loads(secrets.decrypt(session.encrypted_cookies))
    return [item for item in value if isinstance(item, dict)]


def _is_login_page(html: str) -> bool:
    lowered = html.lower().replace(" ", "")
    return "name=\"password\"" in lowered or "name='password'" in lowered


def _is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "challenge-platform" in lowered
        or "just a moment" in lowered
        or "enable javascript and cookies to continue" in lowered
    )


async def refresh_nnmclub(
    database: Database,
    secrets: SecretBox,
    client: FlareSolverrClient,
    username: str,
    password: str,
) -> None:
    namespace = "nnmclub:account:1"
    target = "https://nnmclub.to/forum/index.php"
    with database.session() as session:
        stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
        cookies = _cookie_payload(stored, secrets) if stored else []
    solver_session = "torrwatch-nnmclub-account-1"
    result = await client.request(url=target, session=solver_session, cookies=cookies)
    if _is_login_page(result.html):
        parser = _LoginFormParser()
        parser.feed(result.html)
        action = urljoin(target, parser.action or "/forum/login.php")
        fields = dict(parser.fields)
        fields.update({"username": username, "password": password, "login": "Вход"})
        result = await client.request(
            url=action,
            session=solver_session,
            cookies=result.cookies,
            post_data=urlencode(fields),
        )
        result = await client.request(url=target, session=solver_session, cookies=result.cookies)
    if _is_login_page(result.html) or result.status >= 400:
        raise FlareSolverrError("NNM-Club login did not produce an authenticated page.")
    parsed = httpx.Cookies()
    for item in result.cookies:
        name, value = item.get("name"), item.get("value")
        domain = item.get("domain") or ".nnmclub.to"
        path = item.get("path") or "/"
        if isinstance(name, str) and isinstance(value, str):
            parsed.set(name, value, domain=str(domain), path=str(path))
    SessionStore(database, secrets).save(namespace, parsed, result.user_agent, authenticated=True)
    with database.session() as session:
        stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
        if stored is not None:
            stored.auth_status = "WORKING"
    logger.info("Automatic NNM-Club session refresh completed; cookies=%d", len(list(parsed.jar)))


async def refresh_kinozal(
    database: Database,
    secrets: SecretBox,
    client: FlareSolverrClient,
    username: str,
    password: str,
) -> None:
    """Refresh Kinozal Cloudflare and account session through FlareSolverr."""
    namespace = "kinozal:account:1"
    home = "https://kinozal.me/"
    target = "https://kinozal.me/login.php"
    with database.session() as session:
        stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
        cookies = _cookie_payload(stored, secrets) if stored else []
    solver_session = "torrwatch-kinozal-account-1"
    # A previous browser session can retain a failed Cloudflare challenge.
    # Start clean, then warm up the domain before requesting the login form.
    await client.destroy_session(solver_session)
    result = await client.request(url=home, session=solver_session, cookies=cookies)
    if _is_cloudflare_challenge(result.html) or not _is_login_page(result.html):
        result = await client.request(
            url=target, session=solver_session, cookies=result.cookies
        )
    if _is_login_page(result.html):
        parser = _LoginFormParser()
        parser.feed(result.html)
        action = urljoin(target, parser.action or "/takelogin.php")
        fields = dict(parser.fields)
        fields.update({"username": username, "password": password})
        result = await client.request(
            url=action,
            session=solver_session,
            cookies=result.cookies,
            post_data=urlencode(fields),
        )
        # The POST commonly redirects to takelogin.php and triggers a new
        # Cloudflare check. Request the site again so FlareSolverr can finish
        # that challenge and return the authenticated browser state.
        result = await client.request(url=home, session=solver_session, cookies=result.cookies)
        if _is_cloudflare_challenge(result.html):
            result = await client.request(url=target, session=solver_session, cookies=result.cookies)
    if _is_login_page(result.html) or result.status >= 400 or _is_cloudflare_challenge(result.html):
        raise FlareSolverrError("Kinozal login did not produce an authenticated page.")
    parsed = httpx.Cookies()
    for item in result.cookies:
        name, value = item.get("name"), item.get("value")
        domain = item.get("domain") or ".kinozal.me"
        path = item.get("path") or "/"
        if isinstance(name, str) and isinstance(value, str):
            parsed.set(name, value, domain=str(domain), path=str(path))
    SessionStore(database, secrets).save(namespace, parsed, result.user_agent, authenticated=True)
    with database.session() as session:
        stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
        if stored is not None:
            stored.auth_status = "WORKING"
    logger.info("Automatic Kinozal session refresh completed; cookies=%d", len(list(parsed.jar)))
