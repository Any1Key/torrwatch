"""Typed Telegram and administrator-configured webhook notification adapters."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from torrwatch.notifications.types import (
    NotificationChannelAdapter,
    NotificationChannelConfig,
    NotificationError,
    NotificationErrorCode,
)

ClientFactory = Callable[..., httpx.AsyncClient]


def validate_webhook_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise NotificationError(
            NotificationErrorCode.INVALID_CONFIGURATION, "Webhook endpoint is invalid."
        )
    return value


def telegram_message(payload: dict[str, object]) -> str:
    event = html.escape(str(payload.get("event_type", "SYSTEM_ERROR")))
    message = html.escape(str(payload.get("message", "TorrWatch notification")))
    return f"<b>{event}</b>\n{message}"


class _HttpNotificationAdapter(NotificationChannelAdapter):
    def __init__(
        self, config: NotificationChannelConfig, client_factory: ClientFactory = httpx.AsyncClient
    ) -> None:
        self.config, self.client_factory = config, client_factory

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self.client_factory(
                verify=True, follow_redirects=False, timeout=30.0
            ) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise NotificationError(
                NotificationErrorCode.UNAVAILABLE, "Notification endpoint is unavailable."
            ) from error


class TelegramAdapter(_HttpNotificationAdapter):
    async def send(self, payload: dict[str, object]) -> None:
        token, chat_id = self.config.values["bot_token"], self.config.values["chat_id"]
        data: dict[str, object] = {
            "chat_id": chat_id,
            "text": telegram_message(payload),
            "parse_mode": "HTML",
        }
        if thread_id := self.config.values.get("message_thread_id"):
            data["message_thread_id"] = thread_id
        response = await self._send(
            "POST", f"https://api.telegram.org/bot{token}/sendMessage", json=data
        )
        if response.status_code == 429:
            raise NotificationError(
                NotificationErrorCode.RATE_LIMITED,
                "Telegram rate limit reached.",
                _retry_after(response),
            )
        if response.status_code in {401, 403}:
            raise NotificationError(
                NotificationErrorCode.AUTH_FAILED, "Telegram authentication failed."
            )
        if response.status_code == 400:
            raise NotificationError(
                NotificationErrorCode.DESTINATION_INVALID, "Telegram destination is invalid."
            )
        if response.status_code >= 500:
            raise NotificationError(NotificationErrorCode.UNAVAILABLE, "Telegram is unavailable.")
        try:
            body = response.json()
        except ValueError as error:
            raise NotificationError(
                NotificationErrorCode.PROTOCOL_ERROR, "Telegram returned invalid data."
            ) from error
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise NotificationError(
                NotificationErrorCode.PROTOCOL_ERROR, "Telegram rejected notification."
            )


class WebhookAdapter(_HttpNotificationAdapter):
    async def send(self, payload: dict[str, object]) -> None:
        url = validate_webhook_endpoint(self.config.values["url"])
        headers = {"Content-Type": "application/json"}
        if auth := self.config.values.get("authorization"):
            headers["Authorization"] = auth
        for key, value in json.loads(self.config.values.get("headers_json", "{}")).items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
        response = await self._send(
            "POST", url, headers=headers, content=json.dumps(payload).encode()
        )
        if response.status_code == 429:
            raise NotificationError(
                NotificationErrorCode.RATE_LIMITED,
                "Webhook rate limit reached.",
                _retry_after(response),
            )
        if response.status_code >= 500:
            raise NotificationError(NotificationErrorCode.UNAVAILABLE, "Webhook is unavailable.")
        if response.status_code >= 400:
            raise NotificationError(
                NotificationErrorCode.DESTINATION_INVALID, "Webhook rejected notification."
            )


def adapter_for(config: NotificationChannelConfig) -> NotificationChannelAdapter:
    if config.type.value == "TELEGRAM":
        return TelegramAdapter(config)
    if config.type.value == "WEBHOOK":
        return WebhookAdapter(config)
    raise NotificationError(
        NotificationErrorCode.INVALID_CONFIGURATION, "Unsupported notification channel."
    )


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return max(1, int(response.headers.get("Retry-After", "")))
    except ValueError:
        return None
