from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.trackers.context import PluginHttpClient, ScopedPluginSecrets, TrackerPluginContext
from torrwatch.trackers.loader import load_plugin_registry
from torrwatch.trackers.registry import PluginNotFoundError, PluginRegistrationError, PluginRegistry
from torrwatch.trackers.state import PluginStateNamespace
from torrwatch.trackers.types import (
    PLUGIN_API_VERSION,
    AuthenticationMode,
    PluginCapability,
    PluginHealth,
    RemoteReleaseState,
    TrackerManifest,
    TrackerTarget,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> object:
        from torrwatch.transport.http import TransportResponse

        self.calls.append({"method": method, "url": url, **kwargs})
        return TransportResponse(200, {}, b"fixture title", url, 1, 1)


@dataclass
class FakePlugin:
    manifest: TrackerManifest = TrackerManifest(
        id="fake-tracker",
        display_name="Fake Tracker",
        version="1.0.0",
        plugin_api_version=PLUGIN_API_VERSION,
        core_min_version="1.0.0",
        domains=("tracker.test",),
        auth_modes=(AuthenticationMode.COOKIE,),
        capabilities=(PluginCapability.CHECK, PluginCapability.DOWNLOAD),
    )

    def supports(self, url: str) -> bool:
        parsed = urlsplit(url)
        topic = parsed.path.rstrip("/").removeprefix("/topic/")
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == "tracker.test"
            and parsed.path.startswith("/topic/")
            and topic.isdigit()
        )

    def normalize_url(self, url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(("https", "tracker.test", parsed.path.rstrip("/"), "", ""))

    def extract_external_id(self, url: str) -> str | None:
        return urlsplit(url).path.removeprefix("/topic/")

    async def test_auth(self, ctx: TrackerPluginContext) -> PluginHealth:
        return PluginHealth(
            available=True, authentication_required=ctx.secrets.get("cookie") is None
        )

    async def check(self, target: TrackerTarget, ctx: TrackerPluginContext) -> RemoteReleaseState:
        response = await ctx.http.request("GET", target.canonical_url)  # type: ignore[union-attr]
        return RemoteReleaseState(
            title=response.text,
            external_id=target.external_id,
            canonical_url=target.canonical_url,
            version_key="fixture-v1",
            metadata={"source": "fixture"},
        )

    async def download_torrent(
        self, target: TrackerTarget, state: RemoteReleaseState, ctx: TrackerPluginContext
    ) -> bytes:
        response = await ctx.http.request(  # type: ignore[union-attr]
            "GET",
            state.download_ref or target.canonical_url,
        )
        return response.body


def test_registry_is_deterministic_and_resolves_a_plugin_target() -> None:
    registry = PluginRegistry()
    registry.register(FakePlugin())
    plugin, target = registry.resolve("http://tracker.test/topic/42/?noise=1")
    assert plugin.manifest.id == "fake-tracker"
    assert target == TrackerTarget(
        "http://tracker.test/topic/42/?noise=1", "https://tracker.test/topic/42", "42"
    )
    assert registry.manifests()[0].domains == ("tracker.test",)


def test_registry_rejects_duplicates_and_unsupported_urls() -> None:
    registry = PluginRegistry()
    registry.register(FakePlugin())
    with pytest.raises(PluginRegistrationError, match="Duplicate"):
        registry.register(FakePlugin())
    with pytest.raises(PluginNotFoundError):
        registry.resolve("https://tracker.test/forum/42")
    with pytest.raises(PluginNotFoundError):
        registry.resolve("https://elsewhere.test/topic/42")


def test_registry_rejects_incompatible_plugin_api() -> None:
    plugin = FakePlugin()
    plugin.manifest = TrackerManifest(
        **{**plugin.manifest.__dict__, "plugin_api_version": PLUGIN_API_VERSION + 1}
    )
    with pytest.raises(PluginRegistrationError, match="API"):
        PluginRegistry().register(plugin)


def test_external_discovery_is_metadata_only_and_broken_plugin_is_isolated(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    good = plugins_dir / "good"
    broken = plugins_dir / "broken"
    good.mkdir(parents=True)
    broken.mkdir()
    (good / "manifest.json").write_text(
        json.dumps(
            {
                "id": "external-test",
                "display_name": "External Test",
                "version": "1.0.0",
                "plugin_api_version": 1,
                "core_min_version": "1.0.0",
                "domains": ["external.test"],
                "auth_modes": [],
                "capabilities": ["check"],
            }
        )
    )
    (good / "plugin.py").write_text("raise RuntimeError('must not execute')")
    (broken / "manifest.json").write_text("not json")
    records = PluginRegistry().discover_external(plugins_dir)
    assert [
        (record.manifest.id if record.manifest else None, record.enabled) for record in records
    ] == [
        (None, False),
        ("external-test", False),
    ]
    assert records[0].load_error is not None


def test_external_duplicate_id_is_broken_without_blocking_other_discovery(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    for name in ("one", "two"):
        directory = plugins_dir / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "duplicate-ext",
                    "display_name": name,
                    "version": "1.0.0",
                    "plugin_api_version": 1,
                    "core_min_version": "1.0.0",
                    "domains": [f"{name}.test"],
                }
            )
        )
    records = PluginRegistry().discover_external(plugins_dir)
    assert records[0].manifest is not None
    assert records[1].manifest is None and records[1].load_error is not None


def test_application_loader_discovers_external_metadata_without_executing_code(
    settings: object,
) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    directory = settings.resolved_plugins_dir / "external"  # type: ignore[union-attr]
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "loaded-external",
                "display_name": "Loaded External",
                "version": "1.0.0",
                "plugin_api_version": 1,
                "core_min_version": "1.0.0",
                "domains": ["loaded.test"],
            }
        )
    )
    registry = load_plugin_registry(settings)  # type: ignore[arg-type]
    assert registry.manifests() == ()
    assert registry.external_plugins[0].manifest is not None


def test_namespaced_plugin_state_isolated_and_json_only(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        first = PluginStateNamespace(database, "fake-tracker", "account:one")
        second = PluginStateNamespace(database, "fake-tracker", "account:two")
        first.set("parser-state", {"revision": 1})
        assert first.get("parser-state") == {"revision": 1}
        assert second.get("parser-state") is None
        first.delete("parser-state")
        assert first.get("parser-state") is None
        with pytest.raises(ValueError, match="JSON"):
            first.set("bad", {"object": object()})
    finally:
        database.dispose()


def test_context_is_scoped_and_never_exposes_database_or_secret_box(settings: object) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        context = TrackerPluginContext(
            http=None,  # type: ignore[arg-type]
            state=PluginStateNamespace(database, "fake-tracker"),
            secrets=ScopedPluginSecrets({"cookie": "temporary-value"}),
        )
        assert context.secrets.get("cookie") == "temporary-value"
        assert context.secrets.get("other") is None
        assert not hasattr(context, "database")
        assert not hasattr(context, "secret_box")
    finally:
        database.dispose()


@pytest.mark.asyncio
async def test_scoped_http_binds_domain_session_and_proxy_to_application_policy(
    settings: object,
) -> None:
    ensure_runtime_files(settings)  # type: ignore[arg-type]
    upgrade_database(settings)  # type: ignore[arg-type]
    database = Database(settings.resolved_database_url)  # type: ignore[union-attr]
    try:
        transport = RecordingTransport()
        context = TrackerPluginContext(
            http=PluginHttpClient(  # type: ignore[arg-type]
                transport,
                allowed_hosts={"tracker.test"},
                session_namespace="tracker:fake-tracker:account:1",
                proxy_profile_id=7,
            ),
            state=PluginStateNamespace(database, "fake-tracker", "account:1"),
        )
        target = TrackerTarget("https://tracker.test/topic/1", "https://tracker.test/topic/1", "1")
        result = await FakePlugin().check(target, context)
        assert result.title == "fixture title"
        assert transport.calls == [
            {
                "method": "GET",
                "url": "https://tracker.test/topic/1",
                "headers": None,
                "content": None,
                "data": None,
                "session_namespace": "tracker:fake-tracker:account:1",
                "proxy_profile_id": 7,
                "safe_to_retry": False,
                "allowed_hosts": {"tracker.test"},
            }
        ]
    finally:
        database.dispose()
