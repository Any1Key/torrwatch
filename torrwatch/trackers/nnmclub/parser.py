"""Small defensive NNM-Club topic parser for the release fields TorrWatch uses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


class NnmClubParseError(ValueError):
    pass


class NnmClubAuthenticationRequired(ValueError):
    pass


@dataclass(frozen=True)
class ParsedNnmTopic:
    title: str
    topic_id: str
    download_ref: str
    version_key: str
    source_updated_at: datetime | None


class _NnmDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = False
        self._heading_parts: list[str] = []
        self.title: str | None = None
        self.downloads: list[str] = []
        self.revisions: list[str] = []
        self.updated: list[str] = []
        self.login_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").lower().split())
        if tag == "h1" and ("topic-title" in classes or values.get("id") == "topic-title"):
            self._heading = True
        if tag == "a":
            href = values.get("href", "")
            parsed = urlsplit(href)
            if parsed.path.rstrip("/").endswith("/dl.php") and parse_qs(parsed.query).get("t"):
                self.downloads.append(href)
        if tag == "meta":
            content = values.get("content", "").strip()
            if values.get("name", "").lower() in {"nnm-version", "topic-version"} and content:
                self.revisions.append(content)
            if values.get("property", "").lower() == "article:modified_time" and content:
                self.updated.append(content)
        if values.get("data-last-post-id"):
            self.revisions.append(values["data-last-post-id"])
        if tag == "time" and values.get("datetime"):
            self.updated.append(values["datetime"])
        if (
            tag == "form" and "login" in (values.get("id", "") + values.get("class", "")).lower()
        ) or values.get("name", "").lower() in {"login_username", "login_password"}:
            self.login_form = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._heading:
            self._heading = False
            self.title = " ".join(self._heading_parts).strip() or None

    def handle_data(self, data: str) -> None:
        if self._heading and data.strip():
            self._heading_parts.append(data.strip())


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_topic_page(payload: str, canonical_url: str, topic_id: str) -> ParsedNnmTopic:
    parser = _NnmDocument()
    parser.feed(payload)
    parser.close()
    if parser.login_form:
        raise NnmClubAuthenticationRequired("NNM-Club authentication is required.")
    if not parser.title:
        raise NnmClubParseError("NNM-Club topic title is missing.")
    downloads = [
        href for href in parser.downloads if parse_qs(urlsplit(href).query).get("t") == [topic_id]
    ]
    if len(downloads) != 1:
        raise NnmClubParseError("NNM-Club torrent download reference is missing.")
    updated = next((item for raw in parser.updated if (item := _timestamp(raw))), None)
    version_key = sha256(
        "\x1f".join(
            (
                topic_id,
                parser.title,
                parser.revisions[0] if parser.revisions else "",
                updated.isoformat() if updated else "",
            )
        ).encode()
    ).hexdigest()
    return ParsedNnmTopic(
        parser.title, topic_id, urljoin(canonical_url, downloads[0]), version_key, updated
    )
