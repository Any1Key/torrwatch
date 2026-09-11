"""Narrow persistent non-secret state API for tracker plugins."""

from __future__ import annotations

import json
from typing import Any

from torrwatch.db.database import Database
from torrwatch.db.models import PluginStateRecord


class PluginStateNamespace:
    """A plugin can access only its own declared plugin ID and logical scope."""

    def __init__(self, database: Database, plugin_id: str, scope: str = "global") -> None:
        self._database, self._plugin_id, self._scope = database, plugin_id, scope

    def get(self, key: str) -> Any | None:
        self._validate_key(key)
        with self._database.session() as session:
            stored = (
                session.query(PluginStateRecord)
                .filter_by(plugin_id=self._plugin_id, scope=self._scope, key=key)
                .one_or_none()
            )
            return json.loads(stored.value_json) if stored is not None else None

    def set(self, key: str, value: Any) -> None:
        self._validate_key(key)
        try:
            serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Plugin state must be JSON-serializable.") from error
        with self._database.session() as session:
            stored = (
                session.query(PluginStateRecord)
                .filter_by(plugin_id=self._plugin_id, scope=self._scope, key=key)
                .one_or_none()
            )
            if stored is None:
                session.add(
                    PluginStateRecord(
                        plugin_id=self._plugin_id, scope=self._scope, key=key, value_json=serialized
                    )
                )
            else:
                stored.value_json = serialized

    def delete(self, key: str) -> None:
        self._validate_key(key)
        with self._database.session() as session:
            stored = (
                session.query(PluginStateRecord)
                .filter_by(plugin_id=self._plugin_id, scope=self._scope, key=key)
                .one_or_none()
            )
            if stored is not None:
                session.delete(stored)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or len(key) > 128:
            raise ValueError("Plugin state key must contain 1 to 128 characters.")
