"""HTTP-attempt retry policy; separate from persisted job retries."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: float = 0


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, base_delay_seconds: float = 0.25) -> None:
        self.max_attempts, self.base_delay_seconds = max_attempts, base_delay_seconds

    def decide(
        self,
        *,
        attempt: int,
        method: str,
        safe_to_retry: bool,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ) -> RetryDecision:
        if attempt >= self.max_attempts or (
            method.upper() not in {"GET", "HEAD", "OPTIONS"} and not safe_to_retry
        ):
            return RetryDecision(False)
        retryable = isinstance(error, (httpx.TimeoutException, httpx.NetworkError)) or (
            response is not None
            and (response.status_code == 429 or 500 <= response.status_code <= 599)
        )
        if not retryable:
            return RetryDecision(False)
        if (
            response is not None
            and response.status_code == 429
            and (retry_after := response.headers.get("Retry-After"))
        ):
            try:
                return RetryDecision(True, max(0, float(retry_after)))
            except ValueError:
                try:
                    return RetryDecision(
                        True,
                        max(
                            0,
                            (
                                parsedate_to_datetime(retry_after).astimezone(UTC)
                                - datetime.now(UTC)
                            ).total_seconds(),
                        ),
                    )
                except (TypeError, ValueError):
                    pass
        return RetryDecision(
            True, self.base_delay_seconds * (2 ** (attempt - 1)) * (0.5 + random.random())
        )
