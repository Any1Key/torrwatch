"""One-time browser pairing for encrypted tracker-session import."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import SessionImportPairing, TrackerSession
from torrwatch.transport.cookies import SessionStore


PAIRING_TTL = timedelta(minutes=5)
COOKIE_ALLOWLIST = {
    "nnmclub": frozenset({"phpbb2mysql_4_data", "phpbb2mysql_4_sid", "cf_clearance"})
}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalise_domain(value: str) -> str:
    return value.strip().lower().removeprefix("www.").lstrip(".")


def _domain_allowed(domain: str, allowed_domains: tuple[str, ...]) -> bool:
    host = _normalise_domain(domain)
    return any(host == _normalise_domain(item) for item in allowed_domains)


class SessionImportService:
    def __init__(self, database: Database, secrets_box: SecretBox) -> None:
        self.database = database
        self.secrets_box = secrets_box

    def create_pairing(self, user_id: int, plugin_id: str) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + PAIRING_TTL
        with self.database.session() as session:
            session.execute(
                delete(SessionImportPairing).where(
                    SessionImportPairing.expires_at < datetime.now(UTC)
                )
            )
            session.add(
                SessionImportPairing(
                    token_hash=_hash_token(token),
                    user_id=user_id,
                    plugin_id=plugin_id,
                    expires_at=expires_at,
                )
            )
        return token, expires_at

    def pairing_plugin_id(self, token: str) -> str:
        with self.database.session() as session:
            pairing = session.scalar(
                select(SessionImportPairing).where(
                    SessionImportPairing.token_hash == _hash_token(token)
                )
            )
            if (
                pairing is None
                or pairing.used_at is not None
                or pairing.expires_at <= datetime.now(UTC)
            ):
                raise ValueError("Pairing token is expired or already used.")
            return pairing.plugin_id

    def complete_pairing(
        self,
        token: str,
        domain: str,
        allowed_domains: tuple[str, ...],
        cookies: list[dict[str, Any]],
        user_agent: str,
    ) -> str:
        if not token or len(token) > 256:
            raise ValueError("Invalid pairing token.")
        if not _domain_allowed(domain, allowed_domains):
            raise ValueError("Tracker domain does not match the selected plugin.")
        if not user_agent or len(user_agent) > 1024 or "\r" in user_agent or "\n" in user_agent:
            raise ValueError("Invalid User-Agent.")
        if not isinstance(cookies, list) or not cookies or len(cookies) > 200:
            raise ValueError("No valid tracker cookies were supplied.")

        parsed = httpx.Cookies()
        for item in cookies:
            if not isinstance(item, dict):
                raise ValueError("Invalid tracker cookie payload.")
            name, value = item.get("name"), item.get("value")
            cookie_domain = str(item.get("domain") or domain)
            path = str(item.get("path") or "/")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or not isinstance(value, str)
                or len(value) > 16384
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
                or not _domain_allowed(cookie_domain, allowed_domains)
                or not path.startswith("/")
            ):
                raise ValueError("Invalid tracker cookie payload.")
            parsed.set(name, value, domain=cookie_domain, path=path)

        with self.database.session() as session:
            pairing = session.scalar(
                select(SessionImportPairing).where(
                    SessionImportPairing.token_hash == _hash_token(token)
                )
            )
            now = datetime.now(UTC)
            if pairing is None or pairing.used_at is not None or pairing.expires_at <= now:
                raise ValueError("Pairing token is expired or already used.")
            plugin_id = pairing.plugin_id
            pairing.used_at = now

        allowed_cookie_names = COOKIE_ALLOWLIST.get(plugin_id)
        if allowed_cookie_names is not None:
            filtered = httpx.Cookies()
            for cookie in parsed.jar:
                if cookie.name in allowed_cookie_names:
                    filtered.set(
                        cookie.name,
                        cookie.value,
                        domain=cookie.domain,
                        path=cookie.path,
                    )
            parsed = filtered
            if not list(parsed.jar):
                raise ValueError("No supported NNM-Club session cookies were supplied.")

        namespace = f"{plugin_id}:account:1"
        SessionStore(self.database, self.secrets_box).save(
            namespace, parsed, user_agent, authenticated=False
        )
        with self.database.session() as session:
            stored = session.scalar(
                select(TrackerSession).where(TrackerSession.namespace == namespace)
            )
            if stored is None:
                raise ValueError("Tracker session could not be saved.")
            stored.auth_status = "UNVERIFIED"
            stored.imported_at = datetime.now(UTC)
            stored.last_successful_auth_at = None
        return plugin_id
