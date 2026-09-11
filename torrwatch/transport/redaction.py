"""Secret-safe recursive structured logging helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(character for character in str(key).casefold() if character.isalnum())
    return normalized in {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "password",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "passkey",
        "secret",
        "proxypassword",
    } or normalized.endswith(("token", "secret", "password", "passkey", "apikey"))


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode(
        [
            (key, "[REDACTED]" if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    host = parts.hostname or ""
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
