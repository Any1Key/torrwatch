"""Small defensive NNM-Club topic parser for the release fields TorrWatch uses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit


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
    def __init__(self, canonical_url: str, topic_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_url, self.topic_id = canonical_url, topic_id
        self._heading: str | None = None
        self._ignored = False
        self._heading_parts: list[str] = []
        self.titles: list[str] = []
        self.downloads: list[str] = []
        self.revisions: list[str] = []
        self.updated: list[str] = []
        self.login_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").lower().split())
        if tag in {"script", "style"}:
            self._ignored = True
        is_heading = tag == "h1" and ("topic-title" in classes or values.get("id") == "topic-title")
        if tag == "a":
            href = values.get("href", "")
            parsed = _local_url(href, self.canonical_url)
            if parsed:
                path, query = parsed
                if (
                    "maintitle" in classes
                    and path == "/forum/viewtopic.php"
                    and query.get("t") == [self.topic_id]
                ):
                    is_heading = True
                if path in {"/forum/dl.php", "/forum/download.php"}:
                    self.downloads.append(href)
        if is_heading and self._heading is None:
            self._heading = tag
            self._heading_parts = []
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
        if tag in {"script", "style"}:
            self._ignored = False
        if tag == self._heading:
            self._heading = None
            title = " ".join(self._heading_parts).strip()
            if title:
                self.titles.append(title)

    def handle_data(self, data: str) -> None:
        if self._heading and not self._ignored and data.strip():
            self._heading_parts.append(data.strip())


def _local_url(href: str, canonical_url: str) -> tuple[str, dict[str, list[str]]] | None:
    """Only the observed forum endpoints; transport still validates DNS/redirects."""
    try:
        parsed = urlsplit(urljoin(canonical_url, href))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"nnmclub.to", "www.nnmclub.to"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return None
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return None


def decode_topic_page(body: bytes, headers: Mapping[str, str]) -> str:
    """NNM's observed Windows-1251 and UTF-8 only, without lossy replacement."""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    if charset is None:
        charset = re.search(
            r"<meta\b[^>]*\bcharset\s*=\s*[\"']?([\w-]+)",
            body[:8192].decode("ascii", errors="replace"),
            re.I,
        )
    encoding = charset.group(1).lower() if charset else "utf-8"
    codecs = {
        "windows-1251": "cp1251",
        "cp1251": "cp1251",
        "utf-8": "utf-8-sig",
        "utf8": "utf-8-sig",
    }
    if encoding not in codecs:
        raise NnmClubParseError("NNM-Club page encoding is unsupported.")
    try:
        return body.decode(codecs[encoding])
    except UnicodeDecodeError:
        raise NnmClubParseError("NNM-Club page encoding is invalid.") from None


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_topic_page(payload: str, canonical_url: str, topic_id: str) -> ParsedNnmTopic:
    parser = _NnmDocument(canonical_url, topic_id)
    parser.feed(payload)
    parser.close()
    if parser.login_form:
        raise NnmClubAuthenticationRequired("NNM-Club authentication is required.")
    titles = set(parser.titles)
    if len(titles) != 1:
        raise NnmClubParseError("NNM-Club topic title is missing or ambiguous.")
    title = next(iter(titles))
    downloads: set[str] = set()
    for href in parser.downloads:
        local = _local_url(href, canonical_url)
        if local is None:
            continue
        path, query = local
        key = "t" if path == "/forum/dl.php" else "id"
        ids = query.get(key, [])
        if (
            len(ids) != 1
            or len(ids[0]) > 20
            or not ids[0].isascii()
            or not ids[0].isdigit()
            or int(ids[0]) <= 0
        ):
            continue
        if key == "t" and ids != [topic_id]:
            continue
        # Attachment ID is not the topic ID. Drop session/tracking query fields.
        downloads.add(urlunsplit(("https", "nnmclub.to", path, urlencode({key: ids[0]}), "")))
    if len(downloads) != 1:
        raise NnmClubParseError("NNM-Club torrent download reference is missing.")
    download_ref = next(iter(downloads))
    updated = next((item for raw in parser.updated if (item := _timestamp(raw))), None)
    version_key = sha256(
        "\x1f".join(
            (
                topic_id,
                title,
                download_ref,
                parser.revisions[0] if parser.revisions else "",
                updated.isoformat() if updated else "",
            )
        ).encode()
    ).hexdigest()
    return ParsedNnmTopic(title, topic_id, download_ref, version_key, updated)
