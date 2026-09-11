"""Stable application-owned async HTTP abstraction for future plugins."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx

from torrwatch.core.config import Settings
from torrwatch.transport.cookies import SessionStore
from torrwatch.transport.proxy import ProxyConfigurationError, ProxyService, ResolvedProxy
from torrwatch.transport.rate_limit import DomainRateLimiter
from torrwatch.transport.redaction import redact, redact_url
from torrwatch.transport.retry import RetryPolicy
from torrwatch.transport.url_policy import TrackerUrlPolicy


class TransportError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class TransportRequest:
    method: str
    url: str
    headers: Mapping[str, str] | None = None
    content: bytes | str | None = None
    data: Mapping[str, str] | None = None
    session_namespace: str | None = None
    proxy_profile_id: int | None = None
    safe_to_retry: bool = False
    allowed_hosts: set[str] | None = None


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    elapsed_ms: int
    attempts: int

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")


class HttpTransport:
    """Does policy, cookies, proxy routing and retries without exposing HTTPX client API."""

    def __init__(
        self,
        *,
        proxy_service: ProxyService | None = None,
        sessions: SessionStore | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        timeout: httpx.Timeout | None = None,
        client_factory: object = httpx.AsyncClient,
        url_policy: TrackerUrlPolicy | None = None,
        user_agent: str = "TorrWatch/0.1",
    ) -> None:
        self.proxy_service = proxy_service
        self.sessions = sessions
        self.retry_policy = retry_policy or RetryPolicy()
        self.rate_limiter = rate_limiter or DomainRateLimiter()
        self.timeout = timeout or httpx.Timeout(
            30.0, connect=10.0, read=30.0, write=30.0, pool=10.0
        )
        self.client_factory = client_factory
        self.url_policy = url_policy
        self.user_agent = user_agent

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        proxy_service: ProxyService | None = None,
        sessions: SessionStore | None = None,
    ) -> HttpTransport:
        """Construct the shared transport from bootstrap-only system settings."""
        return cls(
            proxy_service=proxy_service,
            sessions=sessions,
            retry_policy=RetryPolicy(max_attempts=settings.http_max_attempts),
            timeout=httpx.Timeout(
                settings.http_read_timeout_seconds,
                connect=settings.http_connect_timeout_seconds,
                read=settings.http_read_timeout_seconds,
                write=settings.http_write_timeout_seconds,
                pool=settings.http_pool_timeout_seconds,
            ),
            user_agent=settings.http_user_agent,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, str] | None = None,
        session_namespace: str | None = None,
        proxy_profile_id: int | None = None,
        safe_to_retry: bool = False,
        allowed_hosts: set[str] | None = None,
    ) -> TransportResponse:
        return await self.send(
            TransportRequest(
                method=method,
                url=url,
                headers=headers,
                content=content,
                data=data,
                session_namespace=session_namespace,
                proxy_profile_id=proxy_profile_id,
                safe_to_retry=safe_to_retry,
                allowed_hosts=allowed_hosts,
            )
        )

    async def send(self, request: TransportRequest) -> TransportResponse:
        policy = self.url_policy or TrackerUrlPolicy(request.allowed_hosts)
        if self.url_policy is not None and request.allowed_hosts is not None:
            # A per-request allowlist is security-relevant; avoid silently ignoring it.
            policy = TrackerUrlPolicy(request.allowed_hosts, self.url_policy.resolver)
        await policy.validate(request.url)
        try:
            proxies = (
                self.proxy_service.resolve_chain(request.proxy_profile_id)
                if self.proxy_service
                else (ResolvedProxy(None, None),)
            )
        except ProxyConfigurationError as error:
            raise TransportError("proxy", "Configured proxy cannot be used safely.") from error
        cookies, stored_agent = (
            self.sessions.load(request.session_namespace)
            if self.sessions and request.session_namespace
            else (httpx.Cookies(), None)
        )
        headers = {"User-Agent": stored_agent or self.user_agent, **dict(request.headers or {})}
        current_url, attempts, redirects, proxy_index = request.url, 0, 0, 0
        while True:
            attempts += 1
            # DNS can change between bounded HTTP attempts, so do not reuse the
            # decision made for a prior attempt or redirect hop.
            await policy.validate(current_url)
            await self.rate_limiter.wait(urlsplit(current_url).hostname or "")
            started = monotonic()
            try:
                client_kwargs: dict[str, object] = {
                    "timeout": self.timeout,
                    "follow_redirects": False,
                    "verify": True,
                    "cookies": cookies,
                }
                proxy = proxies[proxy_index]
                if proxy.url:
                    client_kwargs["proxy"] = proxy.url
                async with self.client_factory(**client_kwargs) as client:  # type: ignore[operator]
                    response = await client.request(
                        request.method,
                        current_url,
                        headers=headers,
                        content=request.content,
                        data=request.data,
                    )
                    cookies = client.cookies
            except httpx.HTTPError as error:
                if proxies[proxy_index].url and proxy_index + 1 < len(proxies):
                    proxy_index += 1
                    continue
                decision = self.retry_policy.decide(
                    attempt=attempts,
                    method=request.method,
                    safe_to_retry=request.safe_to_retry,
                    error=error,
                )
                if decision.retry:
                    await asyncio.sleep(decision.delay_seconds)
                    continue
                kind = "proxy" if proxies[proxy_index].url else "network_or_timeout"
                raise TransportError(
                    kind, f"Request failed for {redact_url(current_url)}"
                ) from error
            if response.is_redirect and (location := response.headers.get("location")):
                redirects += 1
                if redirects > 10:
                    raise TransportError("redirect", "Too many redirects.")
                current_url = urljoin(current_url, location)
                await policy.validate(current_url)
                continue
            decision = self.retry_policy.decide(
                attempt=attempts,
                method=request.method,
                safe_to_retry=request.safe_to_retry,
                response=response,
            )
            if decision.retry:
                await asyncio.sleep(decision.delay_seconds)
                continue
            if self.sessions and request.session_namespace:
                self.sessions.save(request.session_namespace, cookies, headers["User-Agent"])
            return TransportResponse(
                response.status_code,
                redact(dict(response.headers)),
                response.content,
                redact_url(str(response.url)),
                int((monotonic() - started) * 1000),
                attempts,
            )
