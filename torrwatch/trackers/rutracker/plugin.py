"""RuTracker implementation using only the typed plugin context."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit

from torrwatch.trackers.rutracker.parser import (
    RuTrackerAuthenticationRequired,
    RuTrackerParseError,
    parse_topic_page,
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
from torrwatch.transport.http import TransportError


class RuTrackerPlugin:
    """Trusted built-in support for one explicitly supplied RuTracker topic."""

    manifest = TrackerManifest(
        id="rutracker",
        display_name="RuTracker",
        version="1.0.0",
        plugin_api_version=PLUGIN_API_VERSION,
        core_min_version="0.1.0",
        domains=("rutracker.org",),
        auth_modes=(AuthenticationMode.COOKIE, AuthenticationMode.CREDENTIALS),
        capabilities=(PluginCapability.CHECK, PluginCapability.DOWNLOAD),
    )

    @staticmethod
    def _topic_id(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "rutracker.org",
            "www.rutracker.org",
        }:
            return None
        if parsed.path.rstrip("/") != "/forum/viewtopic.php":
            return None
        values = parse_qs(parsed.query, keep_blank_values=True)
        topics = values.get("t")
        if topics is None or len(topics) != 1 or not topics[0].isdigit() or int(topics[0]) < 1:
            return None
        return topics[0]

    def supports(self, url: str) -> bool:
        return self._topic_id(url) is not None

    def normalize_url(self, url: str) -> str:
        topic_id = self._topic_id(url)
        if topic_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Unsupported RuTracker topic URL."
            )
        return urlunsplit(("https", "rutracker.org", "/forum/viewtopic.php", f"t={topic_id}", ""))

    def extract_external_id(self, url: str) -> str | None:
        return self._topic_id(url)

    async def test_auth(self, ctx: PluginContext) -> PluginHealth:
        try:
            response = await ctx.http.request("GET", "https://rutracker.org/forum/index.php")
        except TransportError:
            return PluginHealth(available=False, message="RuTracker is temporarily unavailable.")
        if response.status_code in {401, 403}:
            return PluginHealth(available=True, authentication_required=True)
        return PluginHealth(available=response.status_code < 500)

    async def check(self, target: TrackerTarget, ctx: PluginContext) -> RemoteReleaseState:
        topic_id = target.external_id or self._topic_id(target.canonical_url)
        if topic_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Invalid RuTracker topic target."
            )
        try:
            response = await ctx.http.request("GET", target.canonical_url)
        except TransportError as error:
            raise _transport_plugin_error(error) from error
        _raise_for_status(response.status_code)
        try:
            parsed = parse_topic_page(response.text, target.canonical_url, topic_id)
        except RuTrackerAuthenticationRequired as error:
            raise TrackerPluginError(
                PluginErrorCode.AUTH_REQUIRED, "RuTracker session is required."
            ) from error
        except RuTrackerParseError as error:
            raise TrackerPluginError(PluginErrorCode.PLUGIN_PARSE_ERROR, str(error)) from error
        return RemoteReleaseState(
            title=parsed.title,
            external_id=parsed.topic_id,
            canonical_url=target.canonical_url,
            version_key=parsed.version_key,
            source_updated_at=parsed.source_updated_at,
            download_ref=parsed.download_ref,
            metadata={"tracker": "rutracker", "topic_id": parsed.topic_id},
        )

    async def download_torrent(
        self, target: TrackerTarget, state: RemoteReleaseState, ctx: PluginContext
    ) -> bytes:
        if not state.download_ref:
            raise TrackerPluginError(
                PluginErrorCode.PLUGIN_PARSE_ERROR,
                "RuTracker torrent download reference is missing.",
            )
        try:
            response = await ctx.http.request("GET", state.download_ref)
        except TransportError as error:
            raise _transport_plugin_error(error) from error
        _raise_for_status(response.status_code)
        return response.body


def _raise_for_status(status_code: int) -> None:
    if status_code == 401:
        raise TrackerPluginError(PluginErrorCode.AUTH_REQUIRED, "RuTracker session is required.")
    if status_code == 404:
        raise TrackerPluginError(PluginErrorCode.INVALID_TARGET, "RuTracker topic was not found.")
    if status_code == 429:
        raise TrackerPluginError(PluginErrorCode.RATE_LIMITED, "RuTracker rate limit was reached.")
    if status_code >= 500:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "RuTracker is temporarily unavailable."
        )
    if status_code >= 400:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "RuTracker request was rejected."
        )


def _transport_plugin_error(error: TransportError) -> TrackerPluginError:
    code = {
        "rate_limit": PluginErrorCode.RATE_LIMITED,
        "proxy": PluginErrorCode.PROXY_ERROR,
    }.get(error.kind, PluginErrorCode.TEMPORARY_NETWORK_ERROR)
    return TrackerPluginError(code, "RuTracker request could not be completed.")
