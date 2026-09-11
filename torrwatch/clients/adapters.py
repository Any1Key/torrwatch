"""HTTP adapters for administrator-configured qBittorrent and Transmission."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx

from torrwatch.clients.types import (
    AddOptions,
    AddResult,
    ClientErrorCode,
    ConnectionResult,
    ExistingTorrent,
    TorrentClientAdapter,
    TorrentClientConfig,
    TorrentClientError,
)

ClientFactory = Callable[..., httpx.AsyncClient]


def validate_admin_endpoint(value: str) -> str:
    """Validate a configured internal endpoint without tracker SSRF restrictions."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TorrentClientError(
            ClientErrorCode.INVALID_CONFIGURATION, "Client endpoint is invalid."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TorrentClientError(
            ClientErrorCode.INVALID_CONFIGURATION, "Client endpoint is invalid."
        )
    return value.rstrip("/") + "/"


class _HttpAdapter(TorrentClientAdapter):
    def __init__(
        self, config: TorrentClientConfig, client_factory: ClientFactory = httpx.AsyncClient
    ) -> None:
        self.config = config
        self._base_url = validate_admin_endpoint(config.base_url)
        self._client_factory = client_factory
        # qBittorrent authenticates with a session cookie.  Keep that state on
        # the adapter, but never persist or expose it outside this execution.
        self._cookies = httpx.Cookies()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client_factory(
                verify=True, follow_redirects=False, timeout=30.0, cookies=self._cookies
            ) as client:
                response = await client.request(
                    method, urljoin(self._base_url, path.lstrip("/")), **kwargs
                )
                self._cookies = client.cookies
                return response
        except httpx.HTTPError as error:
            raise TorrentClientError(
                ClientErrorCode.UNAVAILABLE, "Torrent client is unavailable."
            ) from error


class QBittorrentAdapter(_HttpAdapter):
    async def _login(self) -> None:
        response = await self._request(
            "POST",
            "/api/v2/auth/login",
            data={"username": self.config.username or "", "password": self.config.password or ""},
        )
        if response.status_code >= 500:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "qBittorrent is unavailable.")
        if response.status_code in {401, 403} or response.text.strip().lower() != "ok.":
            raise TorrentClientError(
                ClientErrorCode.AUTH_FAILED, "qBittorrent authentication failed."
            )

    async def test_connection(self) -> ConnectionResult:
        await self._login()
        response = await self._request("GET", "/api/v2/app/version")
        if response.status_code >= 400:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "qBittorrent is unavailable.")
        return ConnectionResult(True)

    async def inspect(self, infohash: str) -> ExistingTorrent | None:
        await self._login()
        response = await self._request("GET", "/api/v2/torrents/info", params={"hashes": infohash})
        if response.status_code >= 400:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "qBittorrent is unavailable.")
        try:
            records = response.json()
        except ValueError as error:
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "qBittorrent returned invalid data."
            ) from error
        if not isinstance(records, list):
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "qBittorrent returned invalid data."
            )
        for record in records:
            if isinstance(record, dict) and str(record.get("hash", "")).lower() == infohash.lower():
                return ExistingTorrent(
                    infohash, str(record.get("name")) if record.get("name") else None
                )
        return None

    async def add(self, torrent: bytes, infohash: str, options: AddOptions) -> AddResult:
        if await self.inspect(infohash):
            return AddResult(infohash, already_present=True)
        data: dict[str, str] = {"paused": "true" if options.paused else "false"}
        if options.save_path:
            data["savepath"] = options.save_path
        if options.category:
            data["category"] = options.category
        if options.tags:
            data["tags"] = ",".join(options.tags)
        response = await self._request(
            "POST",
            "/api/v2/torrents/add",
            data=data,
            files={"torrents": ("release.torrent", torrent)},
        )
        if response.status_code >= 400 or response.text.strip().lower() not in {"ok.", ""}:
            raise TorrentClientError(
                ClientErrorCode.UNAVAILABLE, "qBittorrent did not accept the torrent."
            )
        return AddResult(infohash)

    async def remove(self, infohash: str, *, delete_data: bool = False) -> None:
        if delete_data:
            raise ValueError("Automatic delivery never deletes torrent data.")
        response = await self._request(
            "POST", "/api/v2/torrents/delete", data={"hashes": infohash, "deleteFiles": "false"}
        )
        if response.status_code >= 400:
            raise TorrentClientError(
                ClientErrorCode.UNAVAILABLE, "qBittorrent could not remove the old torrent."
            )


class TransmissionAdapter(_HttpAdapter):
    def __init__(
        self, config: TorrentClientConfig, client_factory: ClientFactory = httpx.AsyncClient
    ) -> None:
        super().__init__(config, client_factory)
        self._session_id: str | None = None

    async def _rpc(self, method: str, arguments: dict[str, object]) -> dict[str, object]:
        headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
        auth = (
            (self.config.username or "", self.config.password or "")
            if self.config.username
            else None
        )
        response = await self._request(
            "POST",
            "/transmission/rpc",
            json={"method": method, "arguments": arguments},
            headers=headers,
            auth=auth,
        )
        if response.status_code == 409:
            session_id = response.headers.get("X-Transmission-Session-Id")
            if not session_id:
                raise TorrentClientError(
                    ClientErrorCode.PROTOCOL_ERROR, "Transmission session negotiation failed."
                )
            self._session_id = session_id
            response = await self._request(
                "POST",
                "/transmission/rpc",
                json={"method": method, "arguments": arguments},
                headers={"X-Transmission-Session-Id": self._session_id},
                auth=auth,
            )
        if response.status_code in {401, 403}:
            raise TorrentClientError(
                ClientErrorCode.AUTH_FAILED, "Transmission authentication failed."
            )
        if response.status_code >= 400:
            raise TorrentClientError(ClientErrorCode.UNAVAILABLE, "Transmission is unavailable.")
        try:
            body = response.json()
        except ValueError as error:
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "Transmission returned invalid data."
            ) from error
        if (
            not isinstance(body, dict)
            or body.get("result") != "success"
            or not isinstance(body.get("arguments"), dict)
        ):
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "Transmission returned invalid data."
            )
        return cast(dict[str, object], body["arguments"])

    async def test_connection(self) -> ConnectionResult:
        await self._rpc("session-get", {})
        return ConnectionResult(True)

    async def inspect(self, infohash: str) -> ExistingTorrent | None:
        body = await self._rpc("torrent-get", {"fields": ["hashString", "name"]})
        records = body.get("torrents", [])
        if not isinstance(records, list):
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "Transmission returned invalid data."
            )
        for record in records:
            if (
                isinstance(record, dict)
                and str(record.get("hashString", "")).lower() == infohash.lower()
            ):
                return ExistingTorrent(
                    infohash, str(record.get("name")) if record.get("name") else None
                )
        return None

    async def add(self, torrent: bytes, infohash: str, options: AddOptions) -> AddResult:
        if await self.inspect(infohash):
            return AddResult(infohash, already_present=True)
        arguments: dict[str, object] = {
            "metainfo": base64.b64encode(torrent).decode("ascii"),
            "paused": options.paused,
        }
        if options.save_path:
            arguments["download-dir"] = options.save_path
        body = await self._rpc("torrent-add", arguments)
        added = body.get("torrent-added") or body.get("torrent-duplicate")
        if not isinstance(added, dict):
            raise TorrentClientError(
                ClientErrorCode.PROTOCOL_ERROR, "Transmission did not confirm the torrent."
            )
        return AddResult(infohash, already_present="torrent-duplicate" in body)

    async def remove(self, infohash: str, *, delete_data: bool = False) -> None:
        if delete_data:
            raise ValueError("Automatic delivery never deletes torrent data.")
        await self._rpc("torrent-remove", {"ids": [infohash], "delete-local-data": False})
