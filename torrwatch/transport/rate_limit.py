"""Small async in-process rate limiter keyed by destination domain."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from time import monotonic


class DomainRateLimiter:
    def __init__(self, interval_seconds: float = 0.0) -> None:
        self.interval_seconds = interval_seconds
        self._next: defaultdict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def wait(self, key: str) -> None:
        async with self._lock:
            now = monotonic()
            target = max(now, self._next[key])
            self._next[key] = target + self.interval_seconds
        if target > now:
            await asyncio.sleep(target - now)
