"""Stable typed public contract for tracker plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from torrwatch.transport.http import TransportResponse

PLUGIN_API_VERSION = 1


class AuthenticationMode(StrEnum):
    COOKIE = "cookie"
    CREDENTIALS = "credentials"


class PluginCapability(StrEnum):
    CHECK = "check"
    DOWNLOAD = "download"


class PluginErrorCode(StrEnum):
    INVALID_TARGET = "INVALID_TARGET"
    UNSUPPORTED_PAGE = "UNSUPPORTED_PAGE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    PLUGIN_PARSE_ERROR = "PLUGIN_PARSE_ERROR"
    TEMPORARY_NETWORK_ERROR = "TEMPORARY_NETWORK_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TRACKER_UNAVAILABLE = "TRACKER_UNAVAILABLE"
    PROXY_ERROR = "PROXY_ERROR"


class TrackerPluginError(RuntimeError):
    """Sanitized, typed failure a plugin may return to application services."""

    def __init__(self, code: PluginErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TrackerManifest:
    id: str
    display_name: str
    version: str
    plugin_api_version: int
    core_min_version: str
    domains: tuple[str, ...]
    auth_modes: tuple[AuthenticationMode, ...] = ()
    capabilities: tuple[PluginCapability, ...] = ()


@dataclass(frozen=True)
class TrackerTarget:
    original_url: str
    canonical_url: str
    external_id: str | None


@dataclass(frozen=True)
class RemoteReleaseState:
    """Sanitized preliminary remote state; it is not torrent-change proof."""

    title: str
    external_id: str | None
    canonical_url: str
    version_key: str | None
    source_updated_at: datetime | None = None
    download_ref: str | None = None
    authentication_required: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginHealth:
    available: bool
    authentication_required: bool = False
    message: str | None = None


@runtime_checkable
class TrackerPlugin(Protocol):
    """Tracker-specific code may only use the scoped context it receives."""

    manifest: TrackerManifest

    def supports(self, url: str) -> bool: ...

    def normalize_url(self, url: str) -> str: ...

    def extract_external_id(self, url: str) -> str | None: ...

    async def test_auth(self, ctx: PluginContext) -> PluginHealth: ...

    async def check(self, target: TrackerTarget, ctx: PluginContext) -> RemoteReleaseState: ...

    async def download_torrent(
        self, target: TrackerTarget, state: RemoteReleaseState, ctx: PluginContext
    ) -> bytes: ...


class PluginState(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...

    def delete(self, key: str) -> None: ...


class PluginSecrets(Protocol):
    def get(self, name: str) -> str | None: ...


class PluginHttp(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        safe_to_retry: bool = False,
    ) -> TransportResponse: ...


class PluginContext(Protocol):
    """Application-owned dependencies; no database or raw HTTP client is exposed."""

    @property
    def http(self) -> PluginHttp: ...

    @property
    def state(self) -> PluginState: ...

    @property
    def secrets(self) -> PluginSecrets: ...
