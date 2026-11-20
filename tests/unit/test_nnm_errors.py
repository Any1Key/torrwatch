from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from torrwatch.trackers.nnmclub.plugin import NnmClubPlugin, _transport_error, _validate_response
from torrwatch.trackers.types import PluginErrorCode, TrackerPluginError
from torrwatch.transport.http import TransportError, TransportResponse


@pytest.mark.parametrize(
    "status,code",
    [
        (401, PluginErrorCode.AUTH_REQUIRED),
        (403, PluginErrorCode.AUTH_REQUIRED),
        (404, PluginErrorCode.INVALID_TARGET),
        (429, PluginErrorCode.RATE_LIMITED),
        (503, PluginErrorCode.TRACKER_UNAVAILABLE),
        (400, PluginErrorCode.TRACKER_UNAVAILABLE),
    ],
)
def test_http_errors_classified(status, code):
    with pytest.raises(TrackerPluginError) as error:
        _validate_response(TransportResponse(status, {}, b"", "https://nnmclub.to", 1, 1))
    assert error.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("status,challenge", [(200, False), (403, False), (200, True)])
async def test_auth_health_does_not_accept_challenge(status, challenge):
    response = TransportResponse(
        status, {"cf-mitigated": "challenge"} if challenge else {}, b"", "https://nnmclub.to", 1, 1
    )
    health = await NnmClubPlugin().test_auth(
        SimpleNamespace(http=SimpleNamespace(request=AsyncMock(return_value=response)))
    )
    assert health is not None
    if challenge:
        assert "challenge" in health.message


@pytest.mark.asyncio
async def test_auth_network_failure_and_safe_error_details():
    error = TransportError("network_or_timeout", "sensitive-url")
    health = await NnmClubPlugin().test_auth(
        SimpleNamespace(http=SimpleNamespace(request=AsyncMock(side_effect=error)))
    )
    assert "unavailable" in health.message
    mapped = _transport_error(error)
    assert mapped.code == PluginErrorCode.TEMPORARY_NETWORK_ERROR
    assert "sensitive-url" not in str(mapped)


@pytest.mark.asyncio
async def test_check_and_download_network_failures_are_classified():
    plugin = NnmClubPlugin()
    target = SimpleNamespace(
        external_id="123", canonical_url="https://nnmclub.to/forum/viewtopic.php?t=123"
    )
    context = SimpleNamespace(
        http=SimpleNamespace(
            request=AsyncMock(side_effect=TransportError("proxy", "private credentials"))
        )
    )
    with pytest.raises(TrackerPluginError) as check:
        await plugin.check(target, context)
    assert check.value.code == PluginErrorCode.PROXY_ERROR
    with pytest.raises(TrackerPluginError) as download:
        await plugin.download_torrent(
            target, SimpleNamespace(download_ref="https://nnmclub.to/forum/dl.php?t=123"), context
        )
    assert download.value.code == PluginErrorCode.PROXY_ERROR
    assert "private credentials" not in str(download.value)


@pytest.mark.asyncio
async def test_invalid_target_and_missing_download_are_rejected_before_network():
    plugin = NnmClubPlugin()
    context = SimpleNamespace(http=SimpleNamespace(request=AsyncMock()))
    with pytest.raises(TrackerPluginError):
        plugin.normalize_url("https://example.org/")
    target = SimpleNamespace(external_id=None, canonical_url="https://example.org/")
    with pytest.raises(TrackerPluginError):
        await plugin.check(target, context)
    with pytest.raises(TrackerPluginError):
        await plugin.download_torrent(target, SimpleNamespace(download_ref=None), context)
    context.http.request.assert_not_called()
