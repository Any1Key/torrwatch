"""Scoped dependencies supplied by application services to tracker plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from torrwatch.trackers.state import PluginStateNamespace
from torrwatch.transport.http import HttpTransport, TransportResponse


class PluginHttpClient:
    """A scoped transport view without proxy/session/allowlist escape hatches."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        allowed_hosts: set[str],
        session_namespace: str | None,
        proxy_profile_id: int | None,
    ) -> None:
        self._transport = transport
        self._allowed_hosts = frozenset(allowed_hosts)
        self._session_namespace = session_namespace
        self._proxy_profile_id = proxy_profile_id

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        safe_to_retry: bool = False,
    ) -> TransportResponse:
        return await self._transport.request(
            method,
            url,
            headers=headers,
            content=content,
            data=data,
            session_namespace=self._session_namespace,
            proxy_profile_id=self._proxy_profile_id,
            safe_to_retry=safe_to_retry,
            allowed_hosts=set(self._allowed_hosts),
        )


@dataclass(frozen=True)
class ScopedPluginSecrets:
    """In-memory, account-scoped values; this is deliberately not a SecretBox."""

    _values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@dataclass(frozen=True)
class TrackerPluginContext:
    http: PluginHttpClient
    state: PluginStateNamespace
    secrets: ScopedPluginSecrets = field(default_factory=ScopedPluginSecrets, repr=False)
