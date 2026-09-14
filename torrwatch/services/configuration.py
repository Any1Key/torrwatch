"""Administrative configuration wiring; all writes stay in existing models."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from torrwatch.clients.adapters import validate_admin_endpoint
from torrwatch.core.secrets import SecretBox
from torrwatch.db.models import (
    MonitorItem,
    NotificationChannel,
    NotificationJob,
    ProxyProfile,
    StoragePath,
    TorrentClient,
    TrackerSession,
)
from torrwatch.domain.enums import (
    MonitorStatus,
    NotificationChannelType,
    NotificationStatus,
    ProxyFallbackMode,
    ProxyType,
    TorrentClientType,
)
from torrwatch.notifications.adapters import validate_webhook_endpoint
from torrwatch.notifications.service import notification_payload
from torrwatch.services.admin import AdminService
from torrwatch.services.jobs import now_utc
from torrwatch.transport.cookies import SessionStore
from torrwatch.transport.proxy import ProxyService


class ConfigurationService:
    def __init__(self, admin: AdminService, secrets: SecretBox) -> None:
        self.admin, self.db, self.secrets = admin, admin.database, secrets

    def sessions(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stored = {row.namespace: row for row in s.scalars(select(TrackerSession))}
            monitors = {row.id: row.plugin_id for row in s.scalars(select(MonitorItem))}
        latest_errors: dict[str, str] = {}
        for event in self.admin.events():
            plugin_id = event["plugin_id"] or monitors.get(event["monitor_id"])
            if plugin_id and event["level"] in {"WARNING", "ERROR", "CRITICAL"}:
                latest_errors.setdefault(plugin_id, event["message"])
        return [
            {
                **item,
                "last_error": latest_errors.get(item["id"]),
                "configured": bool(
                    stored.get(f"{item['id']}:account:1")
                    and stored[f"{item['id']}:account:1"].encrypted_cookies
                ),
            }
            for item in self.admin.trackers()
        ]

    def save(self, resource: str, values: dict[str, str], ident: int | None = None) -> int:
        name = values.get("name", "").strip()
        if resource != "sessions" and (not name or len(name) > 255):
            raise ValueError("name")
        if resource == "sessions":
            plugin = self.admin.registry.get(values["plugin"])
            header = values.get("cookie", "").strip()
            if not header or "\n" in header or "\r" in header or len(header) > 32768:
                raise ValueError("cookie")
            agent = values.get("user_agent", "")
            if "\r" in agent or "\n" in agent or len(agent) > 1024:
                raise ValueError("user_agent")
            SessionStore(self.db, self.secrets).import_cookie_header(
                f"{plugin.manifest.id}:account:1", header, values.get("user_agent") or None
            )
            return 1
        with self.db.session() as s:
            if resource == "paths":
                path = values.get("path", "").strip()
                if not path.startswith("/") or "\x00" in path or "\n" in path or "\r" in path:
                    raise ValueError("path")
                row = s.get(StoragePath, ident) if ident else StoragePath()
                if row is None:
                    raise ValueError("path")
                row.name, row.path, row.enabled = name, path, values.get("enabled") == "on"
                s.add(row)
                s.flush()
                return row.id
            if resource == "clients":
                kind = TorrentClientType(values["type"])
                endpoint = validate_admin_endpoint(values["url"])
                row = s.get(TorrentClient, ident) if ident else TorrentClient()
                if row is None:
                    raise ValueError("client")
                row.name, row.type, row.base_url = name, kind, endpoint
                row.username = values.get("username") or None
                if values.get("password"):
                    row.encrypted_password = self.secrets.encrypt(values["password"])
                row.default_save_path = values.get("save_path") or None
                row.default_category = values.get("category") or None
                row.default_tags_json = json.dumps(
                    [tag.strip() for tag in values.get("tags", "").split(",") if tag.strip()]
                )
                row.enabled = values.get("enabled") == "on"
                s.add(row)
                s.flush()
                return row.id
            if resource == "proxies":
                proxy = s.get(ProxyProfile, ident) if ident else ProxyProfile()
                if proxy is None:
                    raise ValueError("proxy")
                proxy.name, proxy.type = name, ProxyType(values["type"])
                proxy.host = values.get("host") or None
                proxy.port = int(values["port"]) if values.get("port") else None
                proxy.username = values.get("username") or None
                if values.get("password"):
                    proxy.encrypted_password = self.secrets.encrypt(values["password"])
                if proxy.type == ProxyType.DIRECT:
                    proxy.host = proxy.port = proxy.username = proxy.encrypted_password = None
                proxy.enabled = values.get("enabled") == "on"
                proxy.fallback_mode = ProxyFallbackMode(values.get("fallback") or "DISABLED")
                if proxy.fallback_mode == ProxyFallbackMode.PROFILE and not proxy.fallback_proxy_id:
                    raise ValueError("fallback")
                ProxyService(self.db, self.secrets).validate(proxy)
                s.add(proxy)
                s.flush()
                return proxy.id
            if resource == "notifications":
                channel = s.get(NotificationChannel, ident) if ident else NotificationChannel()
                if channel is None:
                    raise ValueError("notification")
                kind_n = NotificationChannelType(values["type"])
                config = (
                    json.loads(self.secrets.decrypt(channel.encrypted_config))
                    if ident and channel.type == kind_n
                    else {}
                )
                if kind_n == NotificationChannelType.TELEGRAM:
                    for key in ("bot_token", "chat_id"):
                        if values.get(key):
                            config[key] = values[key]
                    if not config.get("bot_token") or not config.get("chat_id"):
                        raise ValueError("telegram")
                else:
                    if values.get("url"):
                        config["url"] = validate_webhook_endpoint(values["url"])
                    if not config.get("url"):
                        raise ValueError("url")
                    for key in ("authorization", "headers_json"):
                        if values.get(key):
                            config[key] = values[key]
                    headers = json.loads(config.get("headers_json", "{}"))
                    if not isinstance(headers, dict) or any(
                        not isinstance(k, str)
                        or not isinstance(v, str)
                        or "\n" in k + v
                        or "\r" in k + v
                        for k, v in headers.items()
                    ):
                        raise ValueError("headers")
                events = [
                    v.strip()
                    for v in values.get(
                        "events", "UPDATE_DETECTED,DELIVERY_SUCCESS,DELIVERY_FAILED"
                    ).split(",")
                    if v.strip()
                ]
                allowed = {
                    "UPDATE_DETECTED",
                    "DELIVERY_SUCCESS",
                    "DELIVERY_FAILED",
                    "TRACKER_AUTH_FAILED",
                    "TRACKER_BROKEN",
                    "CLIENT_UNAVAILABLE",
                    "SYSTEM_ERROR",
                }
                if not set(events) <= allowed:
                    raise ValueError("events")
                channel.name, channel.type = name, kind_n
                channel.encrypted_config = self.secrets.encrypt(json.dumps(config))
                channel.event_types_json = json.dumps(events)
                channel.enabled = values.get("enabled") == "on"
                s.add(channel)
                s.flush()
                return channel.id
        raise ValueError("resource")

    def save_monitor(self, values: dict[str, str], ident: int | None = None) -> int:
        name = values.get("name", "").strip()
        interval = int(values.get("interval_seconds", "1800"))
        if len(name) > 255:
            raise ValueError("name")
        if interval < 300:
            raise ValueError("interval_seconds")
        plugin, target = self.admin.registry.resolve(values["url"])
        client_id = int(values["client"]) if values.get("client") else None
        proxy_id = int(values["proxy"]) if values.get("proxy") else None
        with self.db.session() as s:
            client = s.get(TorrentClient, client_id) if client_id else None
            proxy = s.get(ProxyProfile, proxy_id) if proxy_id else None
            if client_id and (client is None or not client.enabled):
                raise ValueError("client")
            if proxy_id and (proxy is None or not proxy.enabled):
                raise ValueError("proxy")
            storage_path = None
            if values.get("storage_path"):
                storage_path = s.get(StoragePath, int(values["storage_path"]))
                if storage_path is None or not storage_path.enabled:
                    raise ValueError("storage_path")
            item = s.get(MonitorItem, ident) if ident else MonitorItem(next_check_at=now_utc())
            if item is None:
                raise ValueError("monitor")
            if ident and item.canonical_url != target.canonical_url:
                raise ValueError("Existing target cannot change; create a new monitor.")
            item.name, item.original_url, item.canonical_url = (
                (
                    item.name
                    if ident
                    else f"{plugin.manifest.display_name} #{target.external_id or 'monitor'}"
                ),
                values["url"],
                target.canonical_url,
            )
            item.plugin_id, item.external_tracker_id = plugin.manifest.id, target.external_id
            item.check_interval_seconds = interval
            item.torrent_client_id, item.proxy_override_id = client_id, proxy_id
            item.client_save_path = storage_path.path if storage_path else None
            if values.get("session") == "shared":
                item.tracker_account_id = 1
            else:
                item.tracker_account_id = None
            item.enabled = values.get("enabled") == "on"
            item.paused = values.get("paused") == "on"
            if not ident:
                item.current_status = MonitorStatus.PAUSED if item.paused else MonitorStatus.HEALTHY
            s.add(item)
            s.flush()
            return item.id

    def test_notification(self, ident: int) -> None:
        with self.db.session() as s:
            row = s.get(NotificationChannel, ident)
            if row is None or not row.enabled:
                raise ValueError("channel")
            s.execute(
                insert(NotificationJob)
                .values(
                    channel_id=ident,
                    event_type="TEST",
                    payload_json=json.dumps(
                        notification_payload(
                            event_type="TEST", message="TorrWatch: тестовое уведомление"
                        )
                    ),
                    status=NotificationStatus.PENDING,
                    active_key=f"notification-test:{ident}",
                    attempts=0,
                    next_attempt_at=now_utc(),
                )
                .on_conflict_do_nothing(index_elements=["active_key"])
            )
