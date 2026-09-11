"""Defensive Kinozal release parser, intentionally limited to TorrWatch state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


class KinozalParseError(ValueError):
    pass


class KinozalAuthenticationRequired(ValueError):
    pass


@dataclass(frozen=True)
class ParsedKinozalRelease:
    title: str
    release_id: str
    download_ref: str
    version_key: str
    source_updated_at: datetime | None


class _KinozalDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture_title = False
        self._title_parts: list[str] = []
        self.title: str | None = None
        self.downloads: list[str] = []
        self.markers: list[str] = []
        self.timestamps: list[str] = []
        self.auth_required = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        element_id = values.get("id", "").lower()
        if tag in {"h1", "h2"} and element_id in {"release-title", "details-title"}:
            self._capture_title = True
        if tag == "a":
            href = values.get("href", "")
            if urlsplit(href).path.rstrip("/").endswith("/download.php"):
                self.downloads.append(href)
        if tag == "meta":
            content = values.get("content", "").strip()
            if values.get("name", "").lower() in {"kinozal-version", "release-version"} and content:
                self.markers.append(content)
            if values.get("property", "").lower() == "article:modified_time" and content:
                self.timestamps.append(content)
        if values.get("data-release-revision"):
            self.markers.append(values["data-release-revision"])
        if tag == "time" and values.get("datetime"):
            self.timestamps.append(values["datetime"])
        login_marker = values.get("name", "").lower()
        if (
            tag == "form" and "login" in (element_id + values.get("class", "")).lower()
        ) or login_marker in {"login", "password"}:
            self.auth_required = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2"} and self._capture_title:
            self._capture_title = False
            self.title = " ".join(self._title_parts).strip() or None

    def handle_data(self, data: str) -> None:
        if self._capture_title and data.strip():
            self._title_parts.append(data.strip())


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_release_page(payload: str, canonical_url: str, release_id: str) -> ParsedKinozalRelease:
    parser = _KinozalDocument()
    parser.feed(payload)
    parser.close()
    if parser.auth_required:
        raise KinozalAuthenticationRequired("Kinozal authentication is required.")
    if not parser.title:
        raise KinozalParseError("Kinozal release title is missing.")
    downloads = [
        href
        for href in parser.downloads
        if parse_qs(urlsplit(href).query).get("id") == [release_id]
    ]
    if len(downloads) != 1:
        raise KinozalParseError("Kinozal torrent download reference is missing.")
    updated = next((item for raw in parser.timestamps if (item := _timestamp(raw))), None)
    version_key = sha256(
        "\x1f".join(
            (
                release_id,
                parser.title,
                parser.markers[0] if parser.markers else "",
                updated.isoformat() if updated else "",
            )
        ).encode()
    ).hexdigest()
    return ParsedKinozalRelease(
        parser.title, release_id, urljoin(canonical_url, downloads[0]), version_key, updated
    )
