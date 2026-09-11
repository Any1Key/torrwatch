"""Typed notification contract; adapters never expose HTTP client internals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from torrwatch.domain.enums import NotificationChannelType


class NotificationErrorCode(StrEnum):
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    AUTH_FAILED = "AUTH_FAILED"
    DESTINATION_INVALID = "DESTINATION_INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


class NotificationError(RuntimeError):
    def __init__(
        self, code: NotificationErrorCode, message: str, retry_after: int | None = None
    ) -> None:
        self.code, self.retry_after = code, retry_after
        super().__init__(message)


@dataclass(frozen=True)
class NotificationChannelConfig:
    id: int
    name: str
    type: NotificationChannelType
    enabled: bool
    event_types: tuple[str, ...]
    values: dict[str, str]


class NotificationChannelAdapter(Protocol):
    async def send(self, payload: dict[str, object]) -> None: ...
