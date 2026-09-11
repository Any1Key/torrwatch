"""SSRF policy for untrusted tracker URLs, distinct from admin service endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str], Awaitable[Iterable[str]]]


class UrlPolicyError(ValueError):
    pass


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list({str(info[4][0]) for info in infos})


def _forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global


class TrackerUrlPolicy:
    """Validates every initial/redirect destination and resolved address."""

    def __init__(
        self, allowed_hosts: set[str] | None = None, resolver: Resolver = system_resolver
    ) -> None:
        self.allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts or set()}
        self.resolver = resolver

    async def validate(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise UrlPolicyError("Tracker URL is malformed.") from error
        if parsed.scheme not in {"http", "https"}:
            raise UrlPolicyError("Only HTTP and HTTPS tracker URLs are permitted.")
        if parsed.username is not None or parsed.password is not None:
            raise UrlPolicyError("Embedded URL credentials are not permitted.")
        host = parsed.hostname
        if not host:
            raise UrlPolicyError("Tracker URL needs a hostname.")
        if port == 0:
            raise UrlPolicyError("Tracker URL has an invalid port.")
        normalized = host.lower().rstrip(".")
        if normalized == "localhost" or normalized.endswith(".localhost"):
            raise UrlPolicyError("Tracker URL resolves to a prohibited network address.")
        if self.allowed_hosts and not any(
            normalized == allowed or normalized.endswith(f".{allowed}")
            for allowed in self.allowed_hosts
        ):
            raise UrlPolicyError("Tracker host is not allowed by the plugin policy.")
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            literal = None
        addresses = [str(literal)] if literal is not None else list(await self.resolver(normalized))
        if not addresses:
            raise UrlPolicyError("Tracker hostname did not resolve.")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as error:
                raise UrlPolicyError("Resolver returned an invalid address.") from error
            if _forbidden(parsed_address):
                raise UrlPolicyError("Tracker URL resolves to a prohibited network address.")
