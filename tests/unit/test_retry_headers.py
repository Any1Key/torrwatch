from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from torrwatch.transport.retry import RetryPolicy


@pytest.mark.parametrize(
    "header",
    [
        "invalid",
        format_datetime(datetime.now(UTC) - timedelta(days=1)),
        format_datetime(datetime.now(UTC) + timedelta(days=1)),
    ],
)
def test_retry_after_dates_and_invalid_header_are_safe(header):
    result = RetryPolicy().decide(
        attempt=1,
        method="GET",
        safe_to_retry=False,
        response=httpx.Response(429, headers={"Retry-After": header}),
    )
    assert result.retry and result.delay_seconds >= 0
    if header == "invalid":
        assert result.delay_seconds < 1
