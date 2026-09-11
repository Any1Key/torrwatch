"""Minimal, defensive parser for the RuTracker topic state needed by TorrWatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


class RuTrackerParseError(ValueError):
    """Required release state is absent or ambiguous in an otherwise non-auth page."""


class RuTrackerAuthenticationRequired(ValueError):
    """The received document is an authentication page, never a release page."""


@dataclass(frozen=True)
class ParsedRuTrackerTopic:
    title: str
    topic_id: str
    download_ref: str
    version_key: str
    source_updated_at: datetime | None


class _TopicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._document_title = False
        self._document_title_parts: list[str] = []
        self.topic_title: str | None = None
        self.download_links: list[str] = []
        self.version_markers: list[str] = []
        self.updated_values: list[str] = []
        self.auth_markers = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        classes = values.get("class", "").lower().split()
        element_id = values.get("id", "").lower()
        name = values.get("name", "").lower()
        if tag == "title":
            self._document_title = True
        if tag in {"h1", "h2"} and (
            "maintitle" in classes or "topic-title" in classes or element_id == "topic-title"
        ):
            self._title_depth += 1
        if tag == "a":
            href = values.get("href", "")
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            if parsed.path.endswith("dl.php") and len(query.get("t", [])) == 1:
                self.download_links.append(href)
        if tag == "meta":
            meta_name = values.get("name", "").lower()
            property_name = values.get("property", "").lower()
            content = values.get("content", "").strip()
            if property_name == "og:title" and content and self.topic_title is None:
                self.topic_title = content
            if meta_name in {"rutracker-version", "topic-version", "last-post-id"} and content:
                self.version_markers.append(content)
            if property_name == "article:modified_time" and content:
                self.updated_values.append(content)
        for key in ("data-topic-updated", "data-last-post-id", "data-version"):
            if values.get(key):
                self.version_markers.append(values[key])
        if (
            tag == "time"
            and (
                "topic" in " ".join(classes)
                or "update" in " ".join(classes)
                or "updated" in element_id
            )
            and values.get("datetime")
        ):
            self.updated_values.append(values["datetime"])
        if (tag == "form" and ("login" in element_id or "login" in classes)) or name in {
            "login_username",
            "login_password",
        }:
            self.auth_markers = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._document_title = False
        if tag in {"h1", "h2"} and self._title_depth:
            self._title_depth -= 1
            if self._title_depth == 0 and self._title_parts:
                self.topic_title = " ".join(self._title_parts).strip()

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if self._title_depth and text:
            self._title_parts.append(text)
        if self._document_title and text:
            self._document_title_parts.append(text)
        if "вход" in text.lower() or "login" in text.lower():
            self.auth_markers = True

    @property
    def document_title(self) -> str | None:
        value = " ".join(self._document_title_parts).strip()
        return value or None


def _parse_timestamp(value: str) -> datetime | None:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_topic_page(payload: str, canonical_url: str, topic_id: str) -> ParsedRuTrackerTopic:
    """Parse only topic title, update marker and torrent reference from untrusted HTML."""

    parser = _TopicPageParser()
    parser.feed(payload)
    parser.close()
    if parser.auth_markers:
        raise RuTrackerAuthenticationRequired("RuTracker authentication is required.")
    title = parser.topic_title or parser.document_title
    if not title:
        raise RuTrackerParseError("RuTracker topic title is missing.")
    matching_links = [
        link
        for link in parser.download_links
        if parse_qs(urlsplit(link).query).get("t") == [topic_id]
    ]
    if len(matching_links) != 1:
        raise RuTrackerParseError("RuTracker torrent download reference is missing.")
    source_updated_at = next(
        (timestamp for value in parser.updated_values if (timestamp := _parse_timestamp(value))),
        None,
    )
    marker = parser.version_markers[0] if parser.version_markers else ""
    fingerprint = "\x1f".join(
        (topic_id, title, marker, source_updated_at.isoformat() if source_updated_at else "")
    )
    return ParsedRuTrackerTopic(
        title=title,
        topic_id=topic_id,
        download_ref=urljoin(canonical_url, matching_links[0]),
        version_key=sha256(fingerprint.encode("utf-8")).hexdigest(),
        source_updated_at=source_updated_at,
    )
