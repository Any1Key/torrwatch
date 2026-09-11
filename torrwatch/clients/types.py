"""Typed, application-owned torrent-client adapter contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from torrwatch.domain.enums import TorrentClientType


class ClientErrorCode(StrEnum):
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    AUTH_FAILED = "AUTH_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


class TorrentClientError(RuntimeError):
    def __init__(self, code: ClientErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TorrentClientConfig:
    id: int
    name: str
    type: TorrentClientType
    base_url: str
    username: str | None
    password: str | None
    default_save_path: str | None = None
    default_category: str | None = None
    default_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddOptions:
    save_path: str | None = None
    category: str | None = None
    tags: tuple[str, ...] = ()
    paused: bool = False


@dataclass(frozen=True)
class ExistingTorrent:
    infohash: str
    name: str | None = None


@dataclass(frozen=True)
class AddResult:
    infohash: str
    already_present: bool = False


@dataclass(frozen=True)
class ConnectionResult:
    connected: bool
    message: str | None = None


class TorrentClientAdapter:
    async def test_connection(self) -> ConnectionResult:
        raise NotImplementedError

    async def inspect(self, infohash: str) -> ExistingTorrent | None:
        raise NotImplementedError

    async def add(self, torrent: bytes, infohash: str, options: AddOptions) -> AddResult:
        raise NotImplementedError

    async def remove(self, infohash: str, *, delete_data: bool = False) -> None:
        raise NotImplementedError
