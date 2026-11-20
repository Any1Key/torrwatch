from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from torrwatch.trackers.registry import PluginNotFoundError, PluginRegistry
from torrwatch.trackers.rutracker import RuTrackerPlugin
from torrwatch.trackers.rutracker.parser import (
    RuTrackerAuthenticationRequired,
    RuTrackerParseError,
    parse_topic_page,
)
from torrwatch.trackers.types import PluginErrorCode, TrackerPluginError
from torrwatch.transport.http import TransportResponse

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rutracker"
TOPIC_URL = "https://rutracker.org/forum/viewtopic.php?t=12345"


@dataclass
class RecordingHttp:
    responses: dict[str, TransportResponse]
    calls: list[str]

    async def request(self, method: str, url: str, **kwargs: object) -> TransportResponse:
        del method, kwargs
        self.calls.append(url)
        return self.responses[url]


@dataclass
class PluginContextFixture:
    http: RecordingHttp
    state: object = object()
    secrets: object = object()


def test_rutracker_plugin_recognizes_only_specific_topic_urls() -> None:
    plugin = RuTrackerPlugin()
    assert plugin.supports("http://www.rutracker.org/forum/viewtopic.php?t=12345&noise=1")
    assert (
        plugin.normalize_url("http://www.rutracker.org/forum/viewtopic.php?t=12345&noise=1")
        == TOPIC_URL
    )
    assert plugin.extract_external_id(TOPIC_URL) == "12345"
    for unsupported in (
        "https://rutracker.org/forum/index.php",
        "https://rutracker.org/forum/tracker.php?nm=test",
        "https://rutracker.org/forum/profile.php",
        "https://rutracker.org/forum/viewtopic.php?t=nope",
        "https://example.test/forum/viewtopic.php?t=12345",
    ):
        assert not plugin.supports(unsupported)


def test_rutracker_registry_registration_and_allowlisted_domain() -> None:
    registry = PluginRegistry()
    registry.register(RuTrackerPlugin())
    plugin, target = registry.resolve("https://www.rutracker.org/forum/viewtopic.php?t=12345")
    assert plugin.manifest.domains == ("rutracker.org",)
    assert target.canonical_url == TOPIC_URL
    with pytest.raises(PluginNotFoundError):
        registry.resolve("https://rutracker.org/forum/index.php")


def test_rutracker_parser_extracts_minimal_sanitized_release_state() -> None:
    parsed = parse_topic_page((FIXTURES / "topic.html").read_text(), TOPIC_URL, "12345")
    assert parsed.title == "Sanitized fixture release"
    assert parsed.topic_id == "12345"
    assert parsed.download_ref == "https://rutracker.org/forum/dl.php?t=12345"
    assert parsed.source_updated_at is not None
    assert len(parsed.version_key) == 64


def test_rutracker_parser_classifies_login_and_broken_markup() -> None:
    with pytest.raises(RuTrackerAuthenticationRequired):
        parse_topic_page((FIXTURES / "login.html").read_text(), TOPIC_URL, "12345")
    with pytest.raises(RuTrackerParseError, match="download"):
        parse_topic_page((FIXTURES / "broken.html").read_text(), TOPIC_URL, "12345")


@pytest.mark.asyncio
async def test_rutracker_plugin_uses_scoped_shared_http_context() -> None:
    plugin = RuTrackerPlugin()
    page = (FIXTURES / "topic.html").read_bytes()
    http = RecordingHttp({TOPIC_URL: TransportResponse(200, {}, page, TOPIC_URL, 1, 1)}, [])
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(TOPIC_URL)
    state = await plugin.check(
        target,
        PluginContextFixture(http),  # type: ignore[arg-type]
    )
    assert state.title == "Sanitized fixture release"
    assert http.calls == [TOPIC_URL]


@pytest.mark.asyncio
async def test_rutracker_plugin_maps_login_page_to_auth_required() -> None:
    plugin = RuTrackerPlugin()
    page = (FIXTURES / "login.html").read_bytes()
    http = RecordingHttp({TOPIC_URL: TransportResponse(200, {}, page, TOPIC_URL, 1, 1)}, [])
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(TOPIC_URL)
    with pytest.raises(TrackerPluginError) as error:
        await plugin.check(
            target,
            PluginContextFixture(http),  # type: ignore[arg-type]
        )
    assert error.value.code == PluginErrorCode.AUTH_REQUIRED
    assert "cookie" not in str(error.value).lower()
