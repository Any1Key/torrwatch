"""Encrypted, isolated persistent cookie/session storage."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import TrackerSession

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class SessionStore:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database, self.secrets = database, secrets

    def load(self, namespace: str) -> tuple[httpx.Cookies, str | None]:
        with self.database.session() as session:
            stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
            if stored is None or not stored.encrypted_cookies:
                return httpx.Cookies(), stored.user_agent if stored else None
            try:
                items = json.loads(self.secrets.decrypt(stored.encrypted_cookies))
            except (TypeError, ValueError) as error:
                raise ValueError("Stored tracker session is invalid.") from error
            if not isinstance(items, list):
                raise ValueError("Stored tracker session is invalid.")
            cookies = httpx.Cookies()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Stored tracker session is invalid.")
                expires = item.get("expires")
                if expires is not None and expires <= _now().timestamp():
                    continue
                try:
                    cookies.set(
                        item["name"], item["value"], domain=item["domain"], path=item["path"]
                    )
                except (KeyError, TypeError) as error:
                    raise ValueError("Stored tracker session is invalid.") from error
            logger.debug(
                "Loaded tracker session namespace=%s cookies=%d user_agent=%s",
                namespace,
                len(cookies),
                bool(stored.user_agent),
            )
            return cookies, stored.user_agent

    def save(
        self,
        namespace: str,
        cookies: httpx.Cookies,
        user_agent: str | None = None,
        authenticated: bool = False,
    ) -> None:
        payload: list[dict[str, object]] = []
        for cookie in cookies.jar:
            payload.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                }
            )
        with self.database.session() as session:
            stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
            if stored is None:
                stored = TrackerSession(namespace=namespace)
                session.add(stored)
            stored.encrypted_cookies = self.secrets.encrypt(
                json.dumps(payload, separators=(",", ":"))
            )
            stored.user_agent = user_agent
            if authenticated:
                stored.last_successful_auth_at = _now()
        logger.debug(
            "Saved tracker session namespace=%s cookies=%d user_agent=%s authenticated=%s",
            namespace,
            len(payload),
            bool(user_agent),
            authenticated,
        )

    def import_cookie_header(
        self, namespace: str, header: str, user_agent: str | None = None
    ) -> None:
        cookies = httpx.Cookies()
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if not separator or not name:
                raise ValueError("Invalid Cookie header.")
            cookies.set(name, value)
        self.save(namespace, cookies, user_agent, authenticated=True)

    def clear(self, namespace: str) -> None:
        with self.database.session() as session:
            stored = session.query(TrackerSession).filter_by(namespace=namespace).one_or_none()
            if stored is not None:
                stored.encrypted_cookies = None
                stored.user_agent = None
                stored.last_successful_auth_at = None
