from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from torrwatch.core.config import Settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox, SecretKeyError
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import ProxyProfile, TrackerSession
from torrwatch.domain.enums import ProxyType
from torrwatch.transport.cookies import SessionStore
from torrwatch.transport.http import HttpTransport
from torrwatch.transport.proxy import ProxyService
from torrwatch.transport.url_policy import TrackerUrlPolicy


def test_secret_box_round_trip_and_wrong_key_fails(tmp_path: Path) -> None:
    first = tmp_path / "first.key"
    second = tmp_path / "second.key"
    first.write_bytes(b"a" * 32)
    second.write_bytes(b"b" * 32)
    ciphertext = SecretBox(first).encrypt("test-proxy-password")
    assert "test-proxy-password" not in ciphertext
    assert SecretBox(first).decrypt(ciphertext) == "test-proxy-password"
    with pytest.raises(SecretKeyError):
        SecretBox(second).decrypt(ciphertext)


def test_secret_box_requires_valid_key(tmp_path: Path) -> None:
    with pytest.raises(SecretKeyError):
        SecretBox(tmp_path / "missing.key")
    invalid = tmp_path / "invalid.key"
    invalid.write_bytes(b"short")
    with pytest.raises(SecretKeyError):
        SecretBox(invalid)


def test_encrypted_sessions_are_isolated_expire_and_clear(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    ensure_runtime_files(settings)
    upgrade_database(settings)
    database = Database(settings.resolved_database_url)
    try:
        store = SessionStore(database, SecretBox(settings.resolved_master_key_file))
        store.import_cookie_header("tracker:one", "sid=first", "browser-one")
        store.import_cookie_header("tracker:two", "sid=second", "browser-two")
        with database.session() as session:
            encrypted = session.query(TrackerSession).filter_by(namespace="tracker:one").one()
            assert encrypted.encrypted_cookies is not None
            assert "sid=first" not in encrypted.encrypted_cookies
        one, one_agent = store.load("tracker:one")
        two, two_agent = store.load("tracker:two")
        assert one.get("sid") == "first"
        assert two.get("sid") == "second"
        assert (one_agent, two_agent) == ("browser-one", "browser-two")
        store.clear("tracker:one")
        cleared, cleared_agent = store.load("tracker:one")
        assert not list(cleared.jar)
        assert cleared_agent is None
    finally:
        database.dispose()


def test_proxy_password_is_encrypted_before_persistence(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    ensure_runtime_files(settings)
    upgrade_database(settings)
    database = Database(settings.resolved_database_url)
    try:
        service = ProxyService(database, SecretBox(settings.resolved_master_key_file))
        with database.session() as session:
            session.add(
                ProxyProfile(
                    name="encrypted",
                    type=ProxyType.HTTP,
                    host="proxy.test",
                    port=8080,
                    username="user",
                    encrypted_password=service.encrypt_password("proxy-password"),
                )
            )
        with database.session() as session:
            profile = session.query(ProxyProfile).filter_by(name="encrypted").one()
            assert profile.encrypted_password is not None
            assert "proxy-password" not in profile.encrypted_password
            assert service.resolve(profile.id).url == "http://user:proxy-password@proxy.test:8080"
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_transport_persists_response_cookies_in_its_namespace(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    ensure_runtime_files(settings)
    upgrade_database(settings)
    database = Database(settings.resolved_database_url)
    seen_cookies: list[str | None] = []
    try:
        store = SessionStore(database, SecretBox(settings.resolved_master_key_file))

        def handler(request: httpx.Request) -> httpx.Response:
            seen_cookies.append(request.headers.get("cookie"))
            return httpx.Response(
                200, headers={"set-cookie": "sid=isolated; Path=/"}, request=request
            )

        def factory(**kwargs: object) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = HttpTransport(
            sessions=store,
            client_factory=factory,
            url_policy=TrackerUrlPolicy(resolver=_public_resolver),
        )
        await transport.request("GET", "https://tracker.test/", session_namespace="tracker:one")
        await transport.request("GET", "https://tracker.test/", session_namespace="tracker:one")
        await transport.request("GET", "https://tracker.test/", session_namespace="tracker:two")
        assert seen_cookies == [None, "sid=isolated", None]
    finally:
        database.dispose()


async def _public_resolver(_: str) -> list[str]:
    return ["93.184.216.34"]
