"""Encrypted persistence boundary for notification configuration."""

from __future__ import annotations

import json

from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import NotificationChannel
from torrwatch.domain.enums import NotificationChannelType
from torrwatch.notifications.types import NotificationChannelConfig


class NotificationChannelRepository:
    def __init__(self, database: Database, secrets: SecretBox) -> None:
        self.database, self.secrets = database, secrets

    def create(
        self,
        *,
        name: str,
        channel_type: NotificationChannelType,
        values: dict[str, str],
        event_types: tuple[str, ...],
    ) -> NotificationChannel:
        self._validate(channel_type, values)
        with self.database.session() as session:
            channel = NotificationChannel(
                name=name,
                type=channel_type,
                encrypted_config=self.secrets.encrypt(json.dumps(values)),
                event_types_json=json.dumps(event_types),
            )
            session.add(channel)
            session.flush()
            return channel

    def config(self, channel_id: int) -> NotificationChannelConfig:
        with self.database.session() as session:
            channel = session.get(NotificationChannel, channel_id)
            if channel is None:
                raise ValueError("Notification channel is unavailable.")
            values = json.loads(self.secrets.decrypt(channel.encrypted_config))
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise ValueError("Notification configuration is invalid.")
            return NotificationChannelConfig(
                channel.id,
                channel.name,
                NotificationChannelType(channel.type),
                channel.enabled,
                tuple(json.loads(channel.event_types_json)),
                values,
            )

    @staticmethod
    def _validate(channel_type: NotificationChannelType, values: dict[str, str]) -> None:
        required = {
            NotificationChannelType.TELEGRAM: {"bot_token", "chat_id"},
            NotificationChannelType.WEBHOOK: {"url"},
        }[channel_type]
        if not required <= set(values) or not all(values[key] for key in required):
            raise ValueError("Notification configuration is invalid.")
