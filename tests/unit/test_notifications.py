from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import NotificationJob
from torrwatch.domain.enums import NotificationChannelType, NotificationStatus
from torrwatch.notifications.adapters import TelegramAdapter, WebhookAdapter, telegram_message
from torrwatch.notifications.repository import NotificationChannelRepository
from torrwatch.notifications.service import NotificationRepository, notification_payload
from torrwatch.notifications.types import (
    NotificationChannelConfig,
    NotificationError,
    NotificationErrorCode,
)
from torrwatch.services.jobs import now_utc


def _factory(handler: httpx.MockTransport):
    return lambda **kwargs: httpx.AsyncClient(transport=handler, **kwargs)


def _config(kind: NotificationChannelType, values: dict[str, str]) -> NotificationChannelConfig:
    return NotificationChannelConfig(1, "test", kind, True, ("UPDATE_DETECTED",), values)


@pytest.mark.asyncio
async def test_telegram_success_escape_and_classification() -> None:
    seen: list[httpx.Request] = []
    adapter = TelegramAdapter(
        _config(NotificationChannelType.TELEGRAM, {"bot_token": "token", "chat_id": "1"}),
        _factory(
            httpx.MockTransport(
                lambda request: (seen.append(request), httpx.Response(200, json={"ok": True}))[1]
            )
        ),
    )
    await adapter.send({"event_type": "UPDATE_DETECTED", "message": "<unsafe>"})
    assert (
        b"&lt;unsafe&gt;" in seen[0].content
        and b"token" not in telegram_message({"message": "x"}).encode()
    )
    bad = TelegramAdapter(
        _config(NotificationChannelType.TELEGRAM, {"bot_token": "secret", "chat_id": "1"}),
        _factory(httpx.MockTransport(lambda _: httpx.Response(401))),
    )
    with pytest.raises(NotificationError) as error:
        await bad.send({})
    assert error.value.code == NotificationErrorCode.AUTH_FAILED and "secret" not in str(
        error.value
    )


@pytest.mark.asyncio
async def test_webhook_payload_headers_and_retry_classification() -> None:
    seen: list[httpx.Request] = []
    adapter = WebhookAdapter(
        _config(
            NotificationChannelType.WEBHOOK,
            {
                "url": "http://192.168.1.10:9000/hook",
                "authorization": "Bearer secret",
                "headers_json": json.dumps({"X-Test": "yes"}),
            },
        ),
        _factory(
            httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(204))[1])
        ),
    )
    payload = notification_payload(event_type="UPDATE_DETECTED", message="safe", release_id=4)
    await adapter.send(payload)
    assert json.loads(seen[0].content)["schema_version"] == 1 and seen[0].headers["x-test"] == "yes"
    limited = WebhookAdapter(
        _config(NotificationChannelType.WEBHOOK, {"url": "https://example.test/hook"}),
        _factory(httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "12"}))),
    )
    with pytest.raises(NotificationError) as error:
        await limited.send(payload)
    assert error.value.code == NotificationErrorCode.RATE_LIMITED and error.value.retry_after == 12


def test_encrypted_repository_and_durable_queue(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        channels = NotificationChannelRepository(
            database, SecretBox(settings.resolved_master_key_file)
        )  # type: ignore[union-attr]
        channel = channels.create(
            name="telegram",
            channel_type=NotificationChannelType.TELEGRAM,
            values={"bot_token": "plaintext-token", "chat_id": "42"},
            event_types=("UPDATE_DETECTED",),
        )
        with database.session() as session:
            assert "plaintext-token" not in session.get(type(channel), channel.id).encrypted_config
        queue = NotificationRepository(database)
        payload = notification_payload(event_type="UPDATE_DETECTED", message="safe")
        assert queue.enqueue_for_event("UPDATE_DETECTED", payload) == 1
        assert queue.enqueue_for_event("UPDATE_DETECTED", payload) == 0
        job = queue.claim_next("a", 60)
        assert job is not None and queue.claim_next("b", 60) is None
        assert queue.renew_lease(job.id, "a", 60)
        assert queue.finish(
            job.id, "a", NotificationError(NotificationErrorCode.UNAVAILABLE, "offline")
        )
        with database.session() as session:
            saved = session.get(NotificationJob, job.id)
            assert saved is not None and saved.status == NotificationStatus.FAILED_RETRYABLE
            saved.next_attempt_at = now_utc() - timedelta(seconds=1)
        retry = queue.claim_next("b", 60)
        assert retry is not None
        assert queue.finish(retry.id, "b")
    finally:
        database.dispose()
