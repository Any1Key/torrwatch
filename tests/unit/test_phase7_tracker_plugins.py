from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from torrwatch.trackers.kinozal import KinozalPlugin
from torrwatch.trackers.kinozal.parser import (
    KinozalAuthenticationRequired,
    KinozalParseError,
    parse_release_page,
)
from torrwatch.trackers.nnmclub import NnmClubPlugin
from torrwatch.trackers.nnmclub.parser import (
    NnmClubAuthenticationRequired,
    NnmClubParseError,
    parse_topic_page,
)
from torrwatch.trackers.registry import PluginNotFoundError, PluginRegistry
from torrwatch.trackers.types import PluginErrorCode, TrackerPluginError
from torrwatch.transport.http import TransportResponse

FIXTURES = Path(__file__).parents[1] / "fixtures"
NNM_URL = "https://nnmclub.to/forum/viewtopic.php?t=23456"
KINOZAL_URL = "https://kinozal.tv/details.php?id=34567"


@dataclass
class RecordingHttp:
    responses: dict[str, TransportResponse]
    calls: list[str]

    async def request(self, method: str, url: str, **kwargs: object) -> TransportResponse:
        del method, kwargs
        self.calls.append(url)
        return self.responses[url]


@dataclass
class Context:
    http: RecordingHttp
    state: object = object()
    secrets: object = object()


@pytest.mark.parametrize(
    ("plugin", "raw", "canonical", "external_id", "unsupported"),
    [
        (
            NnmClubPlugin(),
            "http://www.nnmclub.to/forum/viewtopic.php?t=23456&noise=1",
            NNM_URL,
            "23456",
            "https://nnmclub.to/forum/index.php",
        ),
        (
            KinozalPlugin(),
            "http://www.kinozal.tv/details.php?id=34567&noise=1",
            KINOZAL_URL,
            "34567",
            "https://kinozal.tv/browse.php",
        ),
    ],
)
def test_phase7_urls_are_specific_and_canonical(
    plugin: object, raw: str, canonical: str, external_id: str, unsupported: str
) -> None:
    assert isinstance(plugin, (NnmClubPlugin, KinozalPlugin))
    assert plugin.supports(raw)
    assert plugin.normalize_url(raw) == canonical
    assert plugin.extract_external_id(canonical) == external_id
    assert not plugin.supports(unsupported)
    assert not plugin.supports("https://example.test/details.php?id=34567")


def test_builtin_registry_resolves_each_phase7_domain(settings: object) -> None:
    from torrwatch.trackers.loader import load_plugin_registry

    registry = load_plugin_registry(settings)  # type: ignore[arg-type]
    assert registry.resolve(NNM_URL)[0].manifest.id == "nnmclub"
    assert registry.resolve(KINOZAL_URL)[0].manifest.id == "kinozal"
    with pytest.raises(PluginNotFoundError):
        registry.resolve("https://kinozal.tv/browse.php")


def test_nnm_parser_classifies_auth_and_broken_pages_without_payload_leakage() -> None:
    parsed = parse_topic_page((FIXTURES / "nnmclub" / "topic.html").read_text(), NNM_URL, "23456")
    assert (parsed.title, parsed.topic_id) == ("NNM sanitized release", "23456")
    assert parsed.download_ref == "https://nnmclub.to/forum/dl.php?t=23456"
    assert len(parsed.version_key) == 64 and parsed.source_updated_at is not None
    with pytest.raises(NnmClubAuthenticationRequired):
        parse_topic_page((FIXTURES / "nnmclub" / "login.html").read_text(), NNM_URL, "23456")
    with pytest.raises(NnmClubParseError) as error:
        parse_topic_page((FIXTURES / "nnmclub" / "broken.html").read_text(), NNM_URL, "23456")
    assert "<" not in str(error.value)


def test_kinozal_parser_classifies_auth_and_broken_pages_without_payload_leakage() -> None:
    parsed = parse_release_page(
        (FIXTURES / "kinozal" / "release.html").read_text(), KINOZAL_URL, "34567"
    )
    assert (parsed.title, parsed.release_id) == ("Kinozal sanitized release", "34567")
    assert parsed.download_ref == "https://dl.kinozal.tv/download.php?id=34567"
    assert len(parsed.version_key) == 64 and parsed.source_updated_at is not None
    with pytest.raises(KinozalAuthenticationRequired):
        parse_release_page((FIXTURES / "kinozal" / "login.html").read_text(), KINOZAL_URL, "34567")
    with pytest.raises(KinozalParseError) as error:
        parse_release_page((FIXTURES / "kinozal" / "broken.html").read_text(), KINOZAL_URL, "34567")
    assert "<" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin", "url", "fixture"),
    [
        (NnmClubPlugin(), NNM_URL, "nnmclub/topic.html"),
        (KinozalPlugin(), KINOZAL_URL, "kinozal/release.html"),
    ],
)
async def test_phase7_plugins_use_scoped_transport_and_map_auth(
    plugin: object, url: str, fixture: str
) -> None:
    assert isinstance(plugin, (NnmClubPlugin, KinozalPlugin))
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(url)
    http = RecordingHttp(
        {url: TransportResponse(200, {}, (FIXTURES / fixture).read_bytes(), url, 1, 1)}, []
    )
    state = await plugin.check(target, Context(http))  # type: ignore[arg-type]
    assert state.download_ref and http.calls == [url]

    login_fixture = (
        "nnmclub/login.html" if plugin.manifest.id == "nnmclub" else "kinozal/login.html"
    )
    auth_http = RecordingHttp(
        {url: TransportResponse(200, {}, (FIXTURES / login_fixture).read_bytes(), url, 1, 1)}, []
    )
    with pytest.raises(TrackerPluginError) as error:
        await plugin.check(target, Context(auth_http))  # type: ignore[arg-type]
    assert error.value.code == PluginErrorCode.AUTH_REQUIRED
