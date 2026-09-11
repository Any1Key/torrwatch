"""NNM-Club plugin; all I/O goes through its scoped PluginContext."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit

from torrwatch.trackers.nnmclub.parser import (
    NnmClubAuthenticationRequired,
    NnmClubParseError,
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


class NnmClubPlugin:
    manifest = TrackerManifest(
        id="nnmclub",
        display_name="NNM-Club",
        version="1.0.0",
        plugin_api_version=PLUGIN_API_VERSION,
        core_min_version="0.1.0",
        domains=("nnmclub.to",),
        auth_modes=(AuthenticationMode.COOKIE,),
        capabilities=(PluginCapability.CHECK, PluginCapability.DOWNLOAD),
    )

    @staticmethod
    def _topic_id(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "nnmclub.to",
            "www.nnmclub.to",
        }:
            return None
        if parsed.path.rstrip("/") != "/forum/viewtopic.php":
            return None
        values = parse_qs(parsed.query)
        topics = values.get("t")
        return (
            topics[0]
            if topics and len(topics) == 1 and topics[0].isdigit() and int(topics[0]) > 0
            else None
        )

    def supports(self, url: str) -> bool:
        return self._topic_id(url) is not None

    def normalize_url(self, url: str) -> str:
        topic_id = self._topic_id(url)
        if topic_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Unsupported NNM-Club topic URL."
            )
        return urlunsplit(("https", "nnmclub.to", "/forum/viewtopic.php", f"t={topic_id}", ""))

    def extract_external_id(self, url: str) -> str | None:
        return self._topic_id(url)

    async def test_auth(self, ctx: PluginContext) -> PluginHealth:
        try:
            response = await ctx.http.request("GET", "https://nnmclub.to/forum/index.php")
        except TransportError:
            return PluginHealth(False, message="NNM-Club is temporarily unavailable.")
        return PluginHealth(response.status_code < 500, response.status_code in {401, 403})

    async def check(self, target: TrackerTarget, ctx: PluginContext) -> RemoteReleaseState:
        topic_id = target.external_id or self._topic_id(target.canonical_url)
        if topic_id is None:
            raise TrackerPluginError(
                PluginErrorCode.INVALID_TARGET, "Invalid NNM-Club topic target."
            )
        try:
            response = await ctx.http.request("GET", target.canonical_url)
        except TransportError as error:
            raise _transport_error(error) from error
        _status(response.status_code)
        try:
            parsed = parse_topic_page(response.text, target.canonical_url, topic_id)
        except NnmClubAuthenticationRequired as error:
            raise TrackerPluginError(
                PluginErrorCode.AUTH_REQUIRED, "NNM-Club session is required."
            ) from error
        except NnmClubParseError as error:
            raise TrackerPluginError(PluginErrorCode.PLUGIN_PARSE_ERROR, str(error)) from error
        return RemoteReleaseState(
            parsed.title,
            parsed.topic_id,
            target.canonical_url,
            parsed.version_key,
            parsed.source_updated_at,
            parsed.download_ref,
            metadata={"tracker": "nnmclub", "topic_id": parsed.topic_id},
        )

    async def download_torrent(
        self, target: TrackerTarget, state: RemoteReleaseState, ctx: PluginContext
    ) -> bytes:
        del target
        if not state.download_ref:
            raise TrackerPluginError(
                PluginErrorCode.PLUGIN_PARSE_ERROR,
                "NNM-Club torrent download reference is missing.",
            )
        try:
            response = await ctx.http.request("GET", state.download_ref)
        except TransportError as error:
            raise _transport_error(error) from error
        _status(response.status_code)
        return response.body


def _status(status: int) -> None:
    if status in {401, 403}:
        raise TrackerPluginError(PluginErrorCode.AUTH_REQUIRED, "NNM-Club session is required.")
    if status == 404:
        raise TrackerPluginError(PluginErrorCode.INVALID_TARGET, "NNM-Club topic was not found.")
    if status == 429:
        raise TrackerPluginError(PluginErrorCode.RATE_LIMITED, "NNM-Club rate limit was reached.")
    if status >= 500:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "NNM-Club is temporarily unavailable."
        )
    if status >= 400:
        raise TrackerPluginError(
            PluginErrorCode.TRACKER_UNAVAILABLE, "NNM-Club request was rejected."
        )


def _transport_error(error: TransportError) -> TrackerPluginError:
    code = {"rate_limit": PluginErrorCode.RATE_LIMITED, "proxy": PluginErrorCode.PROXY_ERROR}.get(
        error.kind, PluginErrorCode.TEMPORARY_NETWORK_ERROR
    )
    return TrackerPluginError(code, "NNM-Club request could not be completed.")
