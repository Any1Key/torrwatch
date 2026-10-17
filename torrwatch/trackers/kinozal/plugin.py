"""Kinozal plugin using only application-owned PluginContext services."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit

from torrwatch.trackers.kinozal.parser import (
    KinozalAuthenticationRequired,
    KinozalParseError,
    parse_release_page,
)
from torrwatch.trackers.types import (
    PLUGIN_API_VERSION,
    AuthenticationMode,
    PluginCapability,
    PluginContext,
    PluginErrorCode,
    PluginHealth,
    RemoteReleaseState,
    TrackerManifest,
    TrackerPluginError,
    TrackerTarget,
)
from torrwatch.transport.http import TransportError, TransportResponse


class KinozalPlugin:
    manifest = TrackerManifest(
        id="kinozal",
        display_name="Kinozal",
        version="1.0.1",
        plugin_api_version=PLUGIN_API_VERSION,
        core_min_version="0.1.0",
        domains=(
            "kinozal.tv",
            "dl.kinozal.tv",
            "kinozal.guru",
            "dl.kinozal.guru",
            "kinozal.me",
            "dl.kinozal.me",
        ),
        auth_modes=(AuthenticationMode.COOKIE,),
        capabilities=(PluginCapability.CHECK, PluginCapability.DOWNLOAD),
    )

    @staticmethod
    def _release_id(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "kinozal.tv",
            "www.kinozal.tv",
            "kinozal.guru",
            "www.kinozal.guru",
            "dl.kinozal.tv",
            "dl.kinozal.guru",
            "kinozal.me",
            "www.kinozal.me",
            "dl.kinozal.me",
        }:
            return None
        if parsed.path.rstrip("/") != "/details.php":
            return None
        values = parse_qs(parsed.query)
        release_ids = values.get("id")
        return (
            release_ids[0]
            if release_ids
            and len(release_ids) == 1
            and release_ids[0].isdigit()
            and int(release_ids[0]) > 0
            else None
        )

    def supports(self, url: str) -> bool:
        return self._release_id(url) is not None

    def normalize_url(self, url: str) -> str:
        release_id = self._release_id(url)
        if release_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Unsupported Kinozal release URL."
            )
        host = urlsplit(url).hostname or ""
        if host in {"kinozal.me", "www.kinozal.me", "dl.kinozal.me"}:
            canonical_host = "kinozal.me" if host == "www.kinozal.me" else host
        else:
            canonical_host = "dl.kinozal.guru"
        return urlunsplit(("https", canonical_host, "/details.php", f"id={release_id}", ""))

    def extract_external_id(self, url: str) -> str | None:
        return self._release_id(url)

    async def test_auth(self, ctx: PluginContext) -> PluginHealth:
        try:
            response = await ctx.http.request("GET", "https://dl.kinozal.guru/")
        except TransportError:
            return PluginHealth(False, message="Kinozal is temporarily unavailable.")
        return PluginHealth(response.status_code < 500, response.status_code in {401, 403})

    async def check(self, target: TrackerTarget, ctx: PluginContext) -> RemoteReleaseState:
        release_id = target.external_id or self._release_id(target.canonical_url)
        if release_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Invalid Kinozal release target."
            )
        try:
            response = await ctx.http.request("GET", target.canonical_url)
        except TransportError as error:
            raise _transport_error(error) from error
        _status(response.status_code)
        try:
            parsed = parse_release_page(response.body, target.canonical_url, release_id)
        except KinozalAuthenticationRequired as error:
            raise TrackerPluginError(
                PluginErrorCode.AUTH_REQUIRED, "Kinozal session is required."
            ) from error
        except KinozalParseError as error:
            raise TrackerPluginError(PluginErrorCode.PLUGIN_PARSE_ERROR, str(error)) from error
        return RemoteReleaseState(
            parsed.title,
            parsed.release_id,
            target.canonical_url,
            parsed.version_key,
            parsed.source_updated_at,
            parsed.download_ref,
            metadata={"tracker": "kinozal", "release_id": parsed.release_id},
        )

    async def download_torrent(
        self, target: TrackerTarget, state: RemoteReleaseState, ctx: PluginContext
    ) -> bytes:
        del target
        if not state.download_ref:
            raise TrackerPluginError(
                PluginErrorCode.PLUGIN_PARSE_ERROR, "Kinozal torrent download reference is missing."
            )
        try:
            response = await ctx.http.request("GET", state.download_ref)
        except TransportError as error:
            raise _transport_error(error) from error
        _status(response.status_code)
        _validate_download_response(response)
        return response.body


def _status(status: int) -> None:
    if status in {401, 403}:
        raise TrackerPluginError(PluginErrorCode.AUTH_REQUIRED, "Kinozal session is required.")
    if status == 404:
        raise TrackerPluginError(PluginErrorCode.INVALID_TARGET, "Kinozal release was not found.")
    if status == 429:
        raise TrackerPluginError(PluginErrorCode.RATE_LIMITED, "Kinozal rate limit was reached.")
    if status >= 500:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "Kinozal is temporarily unavailable."
        )
    if status >= 400:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "Kinozal request was rejected."
        )


def _validate_download_response(response: TransportResponse) -> None:
    """Turn Kinozal's HTML download notices into actionable plugin errors."""
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and not response.body.lstrip().startswith(b"<"):
        return
    text = response.body.decode("cp1251", errors="replace").lower()
    if "вам недоступен торрент-файл" in text and "количество торрент-файлов" in text:
        raise TrackerPluginError(
            PluginErrorCode.RATE_LIMITED,
            "Kinozal: дневной лимит скачивания торрент-файлов исчерпан. Дождитесь сброса лимита.",
        )
    raise TrackerPluginError(
        PluginErrorCode.PLUGIN_PARSE_ERROR,
        "Kinozal вернул HTML вместо torrent-файла. Проверьте доступ к скачиванию.",
    )


def _transport_error(error: TransportError) -> TrackerPluginError:
    code = {"rate_limit": PluginErrorCode.RATE_LIMITED, "proxy": PluginErrorCode.PROXY_ERROR}.get(
        error.kind, PluginErrorCode.TEMPORARY_NETWORK_ERROR
    )
    return TrackerPluginError(code, "Kinozal request could not be completed.")
