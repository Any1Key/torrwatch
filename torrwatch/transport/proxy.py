"""Persistent proxy validation, resolution and explicit fallback policy."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from urllib.parse import quote

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import ProxyProfile
from torrwatch.domain.enums import ProxyFallbackMode, ProxyType


class ProxyConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedProxy:
    profile_id: int | None
    url: str | None
    remote_dns: bool = False


@dataclass(frozen=True)
class ProxyTestResult:
    success: bool
    connection_status: str
    http_status: int | None
    latency_ms: int
    error_kind: str | None = None


class ProxyService:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database, self.secrets = database, secrets

    def validate(self, profile: ProxyProfile) -> None:
        if profile.type == ProxyType.DIRECT:
            if (
                profile.host is not None
                or profile.port is not None
                or profile.username
                or profile.encrypted_password
            ):
                raise ProxyConfigurationError("Direct profiles cannot have endpoint credentials.")
        elif not profile.host or profile.port is None or not 1 <= profile.port <= 65535:
            raise ProxyConfigurationError("HTTP and SOCKS5 profiles require a valid host and port.")
        if profile.fallback_mode == ProxyFallbackMode.PROFILE and profile.fallback_proxy_id is None:
            raise ProxyConfigurationError("Profile fallback requires a target profile.")

    def encrypt_password(self, password: str | None) -> str | None:
        """Return the only representation suitable for ``encrypted_password``."""
        return self.secrets.encrypt(password) if password is not None else None

    @staticmethod
    def select_profile_id(
        *,
        monitor_override_id: int | None,
        tracker_account_id: int | None,
        global_default_id: int | None,
    ) -> int | None:
        """Apply the documented assignment precedence without making a network call."""
        return next(
            (
                profile_id
                for profile_id in (monitor_override_id, tracker_account_id, global_default_id)
                if profile_id is not None
            ),
            None,
        )

    def resolve(self, profile_id: int | None) -> ResolvedProxy:
        return self.resolve_chain(profile_id)[0]

    def resolve_chain(self, profile_id: int | None) -> tuple[ResolvedProxy, ...]:
        """Resolve only administrator-authorized fallbacks, detecting every cycle.

        The transport advances this chain only after a proxy connection failure.
        It never invents a direct fallback for a configured profile.
        """
        if profile_id is None:
            return (ResolvedProxy(None, None),)
        seen: set[int] = set()
        routes: list[ResolvedProxy] = []
        with self.database.session() as session:
            current = session.get(ProxyProfile, profile_id)
            while current is not None:
                if current.id in seen:
                    raise ProxyConfigurationError("Proxy fallback loop detected.")
                seen.add(current.id)
                self.validate(current)
                if current.enabled:
                    if current.type == ProxyType.DIRECT:
                        routes.append(ResolvedProxy(current.id, None))
                        return tuple(routes)
                    password = (
                        self.secrets.decrypt(current.encrypted_password)
                        if current.encrypted_password
                        else None
                    )
                    auth = (
                        f"{quote(current.username or '', safe='')}:{quote(password or '', safe='')}@"
                        if current.username or password
                        else ""
                    )
                    scheme = "socks5h" if current.type == ProxyType.SOCKS5 else "http"
                    routes.append(
                        ResolvedProxy(
                            current.id,
                            f"{scheme}://{auth}{current.host}:{current.port}",
                            current.type == ProxyType.SOCKS5,
                        )
                    )
                    if current.fallback_mode == ProxyFallbackMode.DISABLED:
                        return tuple(routes)
                if current.fallback_mode == ProxyFallbackMode.DIRECT:
                    routes.append(ResolvedProxy(None, None))
                    return tuple(routes)
                if current.fallback_mode != ProxyFallbackMode.PROFILE:
                    raise ProxyConfigurationError(
                        "Configured proxy is unavailable; direct fallback is disabled."
                    )
                current = session.get(ProxyProfile, current.fallback_proxy_id)
        raise ProxyConfigurationError("Configured fallback proxy does not exist.")

    async def test(self, profile_id: int, transport: object, url: str) -> ProxyTestResult:
        from torrwatch.transport.http import HttpTransport

        if not isinstance(transport, HttpTransport):
            raise TypeError("Expected application HTTP transport.")
        started = monotonic()
        try:
            response = await transport.request("GET", url, proxy_profile_id=profile_id)
            return ProxyTestResult(
                True, "connected", response.status_code, int((monotonic() - started) * 1000)
            )
        except Exception as error:
            kind = "authentication" if "407" in str(error) else "proxy_or_dns"
            return ProxyTestResult(False, "failed", None, int((monotonic() - started) * 1000), kind)
