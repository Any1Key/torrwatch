"""Automatic tracker-session refresh through a private FlareSolverr service."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

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


class FlareSolverrClient:
    def __init__(self, endpoint: str, timeout_seconds: float = 330.0) -> None:
        self.endpoint = endpoint.rstrip("/") + "/v1"
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._session_commands_supported: bool | None = None

    @staticmethod
    def _session_command_is_unsupported(response: httpx.Response) -> bool:
        """Recognize Byparr's response to unsupported FlareSolverr session commands."""
        if response.status_code != 502:
            return False
        try:
            detail = str(response.json().get("detail") or "")
        except ValueError:
            return False
        return "Invalid url" in detail and '"https://"' in detail

    async def request(
        self,
        *,
        url: str,
        session: str,
        cookies: list[dict[str, object]] | None = None,
        post_data: str | None = None,
        max_timeout_ms: int = 300000,
    ) -> SolverResult:
        payload: dict[str, object] = {
            "cmd": "request.post" if post_data is not None else "request.get",
            "url": url,
            "session": session,
            "session_ttl_minutes": 180,
            "maxTimeout": max_timeout_ms,
            "returnOnlyCookies": False,
            "cookies": cookies or [],
            "waitInSeconds": 2,
        }
        if post_data is not None:
            payload["postData"] = post_data
        # The named RuTracker browser context owns its cookies. Reinjection
        # during a challenge/redirect can fail with invalid cookie domain.
        if session == "torrwatch-rutracker-account-1":
            payload.pop("cookies", None)
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

    async def create_session(self, session: str) -> None:
        """Create a named browser session before using it for requests."""
        if self._session_commands_supported is False:
            return
        payload = {"cmd": "sessions.create", "session": session}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload)
                if self._session_command_is_unsupported(response):
                    self._session_commands_supported = False
                    logger.info(
                        "Solver does not support named browser sessions; using cookie handoff."
                    )
                    return
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise FlareSolverrError("FlareSolverr is unavailable.") from error
        if body.get("status") != "ok":
            message = str(body.get("message") or "FlareSolverr could not create a session.")
            raise FlareSolverrError(message)
        self._session_commands_supported = True

    async def reset_session(self, session: str) -> None:
        """Ensure a fresh named browser session exists.

        FlareSolverr removes sessions after a restart or TTL expiration. A
        request carrying a missing session only returns ``session doesn't
        exist``; recreating it here makes refresh self-healing.
        """
        # FlareSolverr returns HTTP 500 when the named session has already
        # expired or disappeared after a restart. That is exactly the state
        # this method is meant to recover from, so continue with creation.
        try:
            await self.destroy_session(session)
        except FlareSolverrError:
            logger.debug("FlareSolverr session %s was already absent", session)
        await self.create_session(session)

    async def destroy_session(self, session: str) -> None:
        """Drop a possibly stale browser session before a fresh login flow."""
        if self._session_commands_supported is False:
            return
        payload = {"cmd": "sessions.destroy", "session": session}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=payload)
                if self._session_command_is_unsupported(response):
                    self._session_commands_supported = False
                    return
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
    return any(
        marker in lowered
        for marker in (
            'name="password"',
            "name='password'",
            'name="login_password"',
            "name='login_password'",
        )
    )


def _is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "challenge-platform" in lowered
        or "just a moment" in lowered
        or "enable javascript and cookies to continue" in lowered
    )


def _cookie_names(cookies: list[dict[str, object]]) -> set[str]:
    return {
        name
        for item in cookies
        if isinstance((name := item.get("name")), str)
    }


def _require_account_cookies(
    result: SolverResult, required: set[str], tracker: str
) -> None:
    missing = required - _cookie_names(result.cookies)
    if missing:
        raise FlareSolverrError(
            f"{tracker} login did not create account cookies: {', '.join(sorted(missing))}."
        )


async def refresh_nnmclub(
    database: Database,
    secrets: SecretBox,
    client: FlareSolverrClient,
    username: str,
    password: str,
) -> None:
    namespace = "nnmclub:account:1"
    login = "https://nnmclub.to/forum/login.php"
    solver_session = "torrwatch-nnmclub-account-1"
    await client.reset_session(solver_session)
    # Login is one browser operation. A Cloudflare clearance is tied to the
    # browser fingerprint, so handing it to a newly launched browser first can
    # make the challenge unsolvable.
    result = await client.request(
        url=login,
        session=solver_session,
        post_data=urlencode({"username": username, "password": password, "login": "Вход"}),
    )
    if _is_login_page(result.html) or result.status >= 400 or _is_cloudflare_challenge(result.html):
        raise FlareSolverrError("NNM-Club login did not produce an authenticated page.")
    _require_account_cookies(
        result, {"phpbb2mysql_4_data", "phpbb2mysql_4_sid"}, "NNM-Club"
    )
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
    target = "https://kinozal.me/login.php"
    solver_session = "torrwatch-kinozal-account-1"
    # A previous browser session can retain a failed Cloudflare challenge.
    # Start clean, then warm up the domain before requesting the login form.
    await client.reset_session(solver_session)
    result = await client.request(
        url=target,
        session=solver_session,
        post_data=urlencode({"username": username, "password": password}),
    )
    if _is_login_page(result.html) or result.status >= 400 or _is_cloudflare_challenge(result.html):
        raise FlareSolverrError("Kinozal login did not produce an authenticated page.")
    _require_account_cookies(result, {"uid", "pass"}, "Kinozal")
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


async def refresh_rutracker(
    database: Database,
    secrets: SecretBox,
    client: FlareSolverrClient,
    username: str,
    password: str,
) -> None:
    """Refresh RuTracker Cloudflare and account session through FlareSolverr."""
    namespace = "rutracker:account:1"
    login = "https://rutracker.org/forum/login.php"
    solver_session = "torrwatch-rutracker-account-1"
    await client.reset_session(solver_session)
    result = await client.request(
        url=login,
        session=solver_session,
        post_data=urlencode(
            {"login_username": username, "login_password": password, "login": "Вход"}
        ),
    )
    if _is_cloudflare_challenge(result.html):
        raise FlareSolverrError("RuTracker Cloudflare challenge was not completed.")
    if _is_login_page(result.html):
        raise FlareSolverrError(
            "RuTracker запросил дополнительную CAPTCHA при входе или отклонил логин/пароль."
        )
    if result.status >= 400:
        raise FlareSolverrError(f"RuTracker returned HTTP {result.status} after login.")
    _require_account_cookies(result, {"bb_session"}, "RuTracker")
    parsed = httpx.Cookies()
    for item in result.cookies:
        name, value = item.get("name"), item.get("value")
        domain = item.get("domain") or ".rutracker.org"
        path = item.get("path") or "/"
        if isinstance(name, str) and isinstance(value, str):
            parsed.set(name, value, domain=str(domain), path=str(path))
    SessionStore(database, secrets).save(namespace, parsed, result.user_agent, authenticated=True)
    with database.session() as session:
        stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
        if stored is not None:
            stored.auth_status = "WORKING"
    logger.info("Automatic RuTracker session refresh completed; cookies=%d", len(list(parsed.jar)))
