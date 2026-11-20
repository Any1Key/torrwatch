"""Independent offline regression fixtures for the observed NNM forum structure."""

from pathlib import Path

import pytest

from tests.unit.test_phase7_tracker_plugins import Context, RecordingHttp
from torrwatch.trackers.nnmclub import NnmClubPlugin
from torrwatch.trackers.nnmclub.parser import (
    NnmClubParseError,
    decode_topic_page,
    parse_topic_page,
)
from torrwatch.trackers.registry import PluginRegistry
from torrwatch.trackers.types import PluginErrorCode, TrackerPluginError
from torrwatch.transport.http import TransportResponse

URL = "https://nnmclub.to/forum/viewtopic.php?t=23456"
FIXTURE = Path(__file__).parents[1] / "fixtures/nnmclub/forum-topic.html"


def test_nnm_real_structure_cyrillic_and_attachment_identity() -> None:
    html = FIXTURE.read_text()
    decoded = decode_topic_page(html.encode("cp1251"), {})
    parsed = parse_topic_page(decoded, URL, "23456")
    assert parsed.title == "Тестовая раздача fixture-revision"
    assert parsed.topic_id == "23456"
    assert parsed.download_ref == "https://nnmclub.to/forum/download.php?id=98765"
    assert parsed.source_updated_at is None
    assert parsed.version_key == parse_topic_page(decoded, URL, "23456").version_key
    changed = parse_topic_page(decoded.replace("id=98765", "id=98766"), URL, "23456")
    assert changed.version_key != parsed.version_key
    comments = parse_topic_page(decoded.replace("пример.", "другой комментарий."), URL, "23456")
    assert comments.version_key == parsed.version_key


@pytest.mark.parametrize("encoding", ["cp1251", "windows-1251", "utf-8", "utf8"])
def test_nnm_declared_header_encoding(encoding: str) -> None:
    html = "<h1>Проверка кириллицы</h1>"
    assert (
        decode_topic_page(
            html.encode(encoding), {"Content-Type": f'text/html; charset="{encoding}"'}
        )
        == html
    )


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"\xff", {"content-type": "text/html; charset=utf-8"}),
        (b"text", {"content-type": "text/html; charset=utf-7"}),
    ],
)
def test_nnm_invalid_encoding_is_safe_parse_failure(body: bytes, headers: dict[str, str]) -> None:
    with pytest.raises(NnmClubParseError) as exc:
        decode_topic_page(body, headers)
    assert "NNM-Club page encoding" in str(exc.value)


@pytest.mark.parametrize(
    "href",
    [
        "https://example.invalid/download.php?id=98765",
        "http://127.0.0.1/forum/download.php?id=98765",
        "https://user:private@nnmclub.to/forum/download.php?id=98765",
        "download.php?id=0",
        "download.php?id=bad",
        "download.php?id=1&id=2",
        "dl.php?t=99999",
        "download.php?id=",
        "download.php?id=%D9%A1",
        "download.php?id=" + "1" * 4500,
        "https://nnmclub.to:invalid/forum/download.php?id=98765",
    ],
)
def test_nnm_rejects_invalid_download_references(href: str) -> None:
    html = FIXTURE.read_text().replace("download.php?id=98765", href)
    with pytest.raises(NnmClubParseError) as exc:
        parse_topic_page(html, URL, "23456")
    assert "private" not in str(exc.value) and "<" not in str(exc.value)


def test_nnm_requires_bound_title_and_unambiguous_attachment() -> None:
    html = FIXTURE.read_text()
    with pytest.raises(NnmClubParseError):
        parse_topic_page(
            html.replace("viewtopic.php?t=23456", "viewtopic.php?t=11111"), URL, "23456"
        )
    with pytest.raises(NnmClubParseError):
        parse_topic_page(html + '<a href="download.php?id=99999">other</a>', URL, "23456")
    duplicate = parse_topic_page(html + '<a href="download.php?id=98765">same</a>', URL, "23456")
    clean = parse_topic_page(html, URL, "23456")
    assert duplicate == clean
    protected = html.replace("id=98765", "id=98765&amp;sid=synthetic-private-token")
    assert parse_topic_page(protected, URL, "23456") == clean
    script = html.replace("Тестовая", "<script>synthetic-private-token</script>Тестовая")
    assert parse_topic_page(script, URL, "23456") == clean


@pytest.mark.asyncio
async def test_nnm_plugin_decodes_and_downloads_through_context() -> None:
    plugin = NnmClubPlugin()
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(URL)
    torrent = (Path(__file__).parents[1] / "fixtures/torrents/valid-v1.torrent").read_bytes()
    download = "https://nnmclub.to/forum/download.php?id=98765"
    http = RecordingHttp(
        {
            URL: TransportResponse(200, {}, FIXTURE.read_text().encode("cp1251"), URL, 1, 1),
            download: TransportResponse(200, {}, torrent, download, 1, 1),
        },
        [],
    )
    ctx = Context(http)
    remote = await plugin.check(target, ctx)  # type: ignore[arg-type]
    assert remote.title == "Тестовая раздача fixture-revision"
    assert remote.external_id == "23456"
    assert await plugin.download_torrent(target, remote, ctx) == torrent  # type: ignore[arg-type]
    assert http.calls == [URL, download]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,body,code",
    [
        (
            403,
            {"cf-mitigated": "challenge"},
            b"synthetic-private-token",
            PluginErrorCode.TRACKER_UNAVAILABLE,
        ),
        (
            200,
            {},
            b'<script src="/cdn-cgi/challenge-platform/test">_cf_chl_opt</script>',
            PluginErrorCode.TRACKER_UNAVAILABLE,
        ),
        (401, {}, b"private", PluginErrorCode.AUTH_REQUIRED),
        (
            200,
            {},
            b'<form id="login"><input name="login_password"></form>',
            PluginErrorCode.AUTH_REQUIRED,
        ),
    ],
)
async def test_nnm_blocking_is_not_expired_auth(status, headers, body, code) -> None:
    plugin = NnmClubPlugin()
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(URL)
    response = TransportResponse(status, headers, body, URL, 1, 1)
    ctx = Context(RecordingHttp({URL: response}, []))
    with pytest.raises(TrackerPluginError) as exc:
        await plugin.check(target, ctx)  # type: ignore[arg-type]
    assert exc.value.code == code
    assert "private" not in str(exc.value) and "<" not in str(exc.value)


@pytest.mark.asyncio
async def test_nnm_challenge_health_and_download_do_not_trigger_bypass() -> None:
    plugin = NnmClubPlugin()
    registry = PluginRegistry()
    registry.register(plugin)
    _, target = registry.resolve(URL)
    download = "https://nnmclub.to/forum/download.php?id=98765"
    index = "https://nnmclub.to/forum/index.php"
    blocked = TransportResponse(403, {"cf-mitigated": "challenge"}, b"private", download, 1, 1)
    http = RecordingHttp(
        {
            URL: TransportResponse(200, {}, FIXTURE.read_text().encode("cp1251"), URL, 1, 1),
            index: blocked,
            download: blocked,
        },
        [],
    )
    ctx = Context(http)
    health = await plugin.test_auth(ctx)  # type: ignore[arg-type]
    assert not health.available and not health.authentication_required
    remote = await plugin.check(target, ctx)  # type: ignore[arg-type]
    with pytest.raises(TrackerPluginError) as exc:
        await plugin.download_torrent(target, remote, ctx)  # type: ignore[arg-type]
    assert exc.value.code == PluginErrorCode.TRACKER_UNAVAILABLE
    assert http.calls == [index, URL, download]
