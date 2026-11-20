from __future__ import annotations

import json

import httpx
import pytest

from torrwatch.clients.adapters import (
    QBittorrentAdapter,
    TransmissionAdapter,
    validate_admin_endpoint,
)
from torrwatch.clients.types import (
    AddOptions,
    ClientErrorCode,
    TorrentClientConfig,
    TorrentClientError,
)
from torrwatch.domain.enums import TorrentClientType


def factory(handler: httpx.MockTransport):
    return lambda **kwargs: httpx.AsyncClient(transport=handler, **kwargs)


def config(kind: TorrentClientType) -> TorrentClientConfig:
    return TorrentClientConfig(1, "test", kind, "http://192.168.1.50:8080", "user", "secret")


def test_admin_endpoint_accepts_lan_and_rejects_unsafe_forms() -> None:
    assert validate_admin_endpoint("http://192.168.1.50:8080") == "http://192.168.1.50:8080/"
    for value in ("ftp://host", "http://user:pass@host", "http://host/?x=1"):
        with pytest.raises(TorrentClientError):
            validate_admin_endpoint(value)


@pytest.mark.asyncio
async def test_qbittorrent_add_verify_and_safe_removal() -> None:
    calls: list[tuple[str, str, bytes]] = []
    state = {"old": True, "new": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("info"):
            key = request.url.params["hashes"]
            return httpx.Response(200, json=[{"hash": key}] if state.get(key) else [])
        if request.url.path.endswith("add"):
            assert (
                b"savepath" in request.content
                and b"category" in request.content
                and b"tags" in request.content
            )
            state["new"] = True
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("delete"):
            assert b"deleteFiles=false" in request.content
            state["old"] = False
            return httpx.Response(200, text="Ok.")
        return httpx.Response(200, text="1.0")

    adapter = QBittorrentAdapter(
        config(TorrentClientType.QBITTORRENT), factory(httpx.MockTransport(handler))
    )
    await adapter.test_connection()
    assert await adapter.inspect("old") is not None
    assert not (
        await adapter.add(b"torrent", "new", AddOptions("/save", "cat", ("one", "two")))
    ).already_present
    assert await adapter.inspect("new") is not None
    await adapter.remove("old")
    assert state == {"old": False, "new": True}
    assert calls.index(next(call for call in calls if call[1].endswith("add"))) < calls.index(
        next(call for call in calls if call[1].endswith("delete"))
    )


@pytest.mark.asyncio
async def test_qbittorrent_auth_and_network_failures_are_sanitized() -> None:
    bad = QBittorrentAdapter(
        config(TorrentClientType.QBITTORRENT),
        factory(httpx.MockTransport(lambda _: httpx.Response(403))),
    )
    with pytest.raises(TorrentClientError) as error:
        await bad.test_connection()
    assert error.value.code == ClientErrorCode.AUTH_FAILED and "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_transmission_negotiates_adds_duplicates_and_preserves_data() -> None:
    calls: list[dict[str, object]] = []
    session = False
    state = {"old": True, "new": False}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal session
        if not session:
            session = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "fixture"})
        body = json.loads(request.content)
        calls.append(body)
        method, args = body["method"], body["arguments"]
        if method == "session-get":
            result = {}
        elif method == "torrent-get":
            result = {
                "torrents": [{"hashString": name} for name, exists in state.items() if exists]
            }
        elif method == "torrent-add":
            assert args["download-dir"] == "/save"
            state["new"] = True
            result = {"torrent-added": {"id": 1}}
        elif method == "torrent-remove":
            assert args["delete-local-data"] is False
            state["old"] = False
            result = {}
        else:
            result = {}
        return httpx.Response(200, json={"result": "success", "arguments": result})

    adapter = TransmissionAdapter(
        config(TorrentClientType.TRANSMISSION), factory(httpx.MockTransport(handler))
    )
    assert (await adapter.test_connection()).connected
    await adapter.add(b"torrent", "new", AddOptions("/save"))
    assert await adapter.inspect("new") is not None
    await adapter.remove("old")
    assert state == {"old": False, "new": True}
    assert [item["method"] for item in calls].index("torrent-add") < [
        item["method"] for item in calls
    ].index("torrent-remove")
