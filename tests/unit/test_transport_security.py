from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from torrwatch.core.config import Settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import ProxyProfile
from torrwatch.domain.enums import ProxyFallbackMode, ProxyType
from torrwatch.transport.http import HttpTransport, TransportError
from torrwatch.transport.proxy import ProxyConfigurationError, ProxyService
from torrwatch.transport.rate_limit import DomainRateLimiter
from torrwatch.transport.redaction import redact, redact_url
from torrwatch.transport.retry import RetryPolicy
from torrwatch.transport.url_policy import TrackerUrlPolicy, UrlPolicyError


async def public_resolver(_: str) -> list[str]:
    return ["93.184.216.34"]


async def private_resolver(_: str) -> list[str]:
    return ["127.0.0.1"]


async def redirect_resolver(host: str) -> list[str]:
    return ["127.0.0.1"] if host == "private.test" else ["93.184.216.34"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "file:///etc/passwd",
        "gopher://127.0.0.1/",
    ],
)
async def test_url_policy_rejects_ssrf_targets(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        await TrackerUrlPolicy(resolver=private_resolver).validate(url)


@pytest.mark.asyncio
async def test_url_policy_allowlist_and_dns_validation() -> None:
    policy = TrackerUrlPolicy({"example.test"}, public_resolver)
    await policy.validate("https://api.example.test/path")
    with pytest.raises(UrlPolicyError):
        await policy.validate("https://other.test/")
    with pytest.raises(UrlPolicyError):
        await TrackerUrlPolicy(resolver=private_resolver).validate("https://example.test/")


@pytest.mark.asyncio
async def test_transport_revalidates_redirect_before_requesting_it() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://private.test/"}, request=request)

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    transport = HttpTransport(
        client_factory=factory,
        url_policy=TrackerUrlPolicy(resolver=redirect_resolver),
    )
    with pytest.raises(UrlPolicyError):
        await transport.request("GET", "https://public.test/")
    assert calls == ["https://public.test/"]


@pytest.mark.asyncio
async def test_transport_revalidates_dns_before_a_retry() -> None:
    resolutions = 0

    async def changing_resolver(_: str) -> list[str]:
        nonlocal resolutions
        resolutions += 1
        return ["93.184.216.34"] if resolutions < 3 else ["127.0.0.1"]

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request)),
            **kwargs,
        )

    transport = HttpTransport(
        client_factory=factory,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
        url_policy=TrackerUrlPolicy(resolver=changing_resolver),
    )
    with pytest.raises(UrlPolicyError):
        await transport.request("GET", "https://public.test/")
    assert resolutions == 3


def test_retry_classifies_transient_and_safe_post() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0)
    assert policy.decide(
        attempt=1, method="GET", safe_to_retry=False, error=httpx.ReadTimeout("x")
    ).retry
    assert not policy.decide(
        attempt=1, method="POST", safe_to_retry=False, error=httpx.ReadTimeout("x")
    ).retry
    response = httpx.Response(429, headers={"Retry-After": "2"})
    assert (
        policy.decide(attempt=1, method="GET", safe_to_retry=False, response=response).delay_seconds
        == 2
    )
    assert not policy.decide(
        attempt=1, method="GET", safe_to_retry=False, response=httpx.Response(404)
    ).retry


def test_transport_uses_configured_timeouts_retries_and_user_agent(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=2.0,
        http_write_timeout_seconds=3.0,
        http_pool_timeout_seconds=4.0,
        http_max_attempts=4,
        http_user_agent="TorrWatch-test/1",
    )
    transport = HttpTransport.from_settings(settings)
    assert transport.retry_policy.max_attempts == 4
    assert transport.timeout.connect == 1.0
    assert transport.timeout.read == 2.0
    assert transport.timeout.write == 3.0
    assert transport.timeout.pool == 4.0
    assert transport.user_agent == "TorrWatch-test/1"


def test_redaction_is_recursive_and_masks_sensitive_query() -> None:
    value = redact(
        {
            "Authorization": "secret",
            "nested": [{"password": "bad", "refreshToken": "also-bad"}],
            "safe": "yes",
        }
    )
    assert value == {
        "Authorization": "[REDACTED]",
        "nested": [{"password": "[REDACTED]", "refreshToken": "[REDACTED]"}],
        "safe": "yes",
    }
    assert "abc" not in redact_url("https://example.test/?passkey=abc&safe=yes")


@pytest.mark.asyncio
async def test_rate_limit_waits_without_blocking_event_loop() -> None:
    limiter = DomainRateLimiter(0.03)
    await limiter.wait("example.test")
    marker = False

    async def tick() -> None:
        nonlocal marker
        await asyncio.sleep(0)
        marker = True

    await asyncio.gather(limiter.wait("example.test"), tick())
    assert marker


@pytest.mark.asyncio
async def test_rate_limit_sleep_is_cancellation_aware() -> None:
    limiter = DomainRateLimiter(10)
    await limiter.wait("example.test")
    task = asyncio.create_task(limiter.wait("example.test"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _database(tmp_path: Path) -> tuple[Database, SecretBox]:
    from torrwatch.core.config import Settings

    settings = Settings(data_dir=tmp_path / "data")
    ensure_runtime_files(settings)
    upgrade_database(settings)
    return Database(settings.resolved_database_url), SecretBox(settings.resolved_master_key_file)


def test_proxy_fallback_is_explicit_and_detects_loops(tmp_path: Path) -> None:
    database, secrets = _database(tmp_path)
    try:
        with database.session() as session:
            blocked = ProxyProfile(
                name="blocked", type=ProxyType.HTTP, host="proxy.test", port=8080, enabled=True
            )
            fallback = ProxyProfile(
                name="fallback", type=ProxyType.SOCKS5, host="socks.test", port=1080, enabled=True
            )
            session.add_all([blocked, fallback])
        with database.session() as session:
            blocked = session.query(ProxyProfile).filter_by(name="blocked").one()
            fallback = session.query(ProxyProfile).filter_by(name="fallback").one()
            blocked.fallback_mode = ProxyFallbackMode.PROFILE
            blocked.fallback_proxy_id = fallback.id
        routes = ProxyService(database, secrets).resolve_chain(blocked.id)
        assert [route.url for route in routes] == [
            "http://proxy.test:8080",
            "socks5h://socks.test:1080",
        ]
        assert (
            ProxyService.select_profile_id(
                monitor_override_id=3, tracker_account_id=2, global_default_id=1
            )
            == 3
        )
        assert (
            ProxyService.select_profile_id(
                monitor_override_id=None, tracker_account_id=2, global_default_id=1
            )
            == 2
        )
        assert (
            ProxyService.select_profile_id(
                monitor_override_id=None, tracker_account_id=None, global_default_id=1
            )
            == 1
        )
        with database.session() as session:
            fallback = session.query(ProxyProfile).filter_by(name="fallback").one()
            fallback.fallback_mode = ProxyFallbackMode.PROFILE
            fallback.fallback_proxy_id = blocked.id
        with pytest.raises(ProxyConfigurationError, match="loop"):
            ProxyService(database, secrets).resolve_chain(blocked.id)
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_proxy_failure_never_bypasses_to_direct_without_explicit_fallback(
    tmp_path: Path,
) -> None:
    database, secrets = _database(tmp_path)
    try:
        with database.session() as session:
            profile = ProxyProfile(
                name="required", type=ProxyType.HTTP, host="proxy.test", port=8080, enabled=True
            )
            session.add(profile)
        proxy_id = profile.id
        calls: list[str | None] = []

        def factory(**kwargs: object) -> httpx.AsyncClient:
            calls.append(kwargs.get("proxy") if isinstance(kwargs.get("proxy"), str) else None)
            return httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: (_ for _ in ()).throw(httpx.ProxyError("down"))
                ),
                **kwargs,
            )

        transport = HttpTransport(
            proxy_service=ProxyService(database, secrets),
            client_factory=factory,
            retry_policy=RetryPolicy(max_attempts=1),
            url_policy=TrackerUrlPolicy(resolver=public_resolver),
        )
        with pytest.raises(TransportError, match="Request failed") as error:
            await transport.request("GET", "https://tracker.test/", proxy_profile_id=proxy_id)
        assert error.value.kind == "proxy"
        assert calls == ["http://proxy.test:8080"]
    finally:
        database.dispose()
