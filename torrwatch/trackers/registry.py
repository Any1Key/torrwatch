"""Deterministic registration and safe metadata discovery for tracker plugins."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from torrwatch.trackers.types import (
    PLUGIN_API_VERSION,
    AuthenticationMode,
    PluginCapability,
    PluginErrorCode,
    TrackerManifest,
    TrackerPlugin,
    TrackerPluginError,
    TrackerTarget,
)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class PluginRegistrationError(ValueError):
    pass


class PluginNotFoundError(TrackerPluginError):
    def __init__(self) -> None:
        super().__init__(PluginErrorCode.UNSUPPORTED_PAGE, "No enabled plugin supports this URL.")


@dataclass(frozen=True)
class ExternalPluginRecord:
    path: Path
    manifest: TrackerManifest | None
    enabled: bool
    load_error: str | None = None


class PluginRegistry:
    """Registry contains implementations only; it has no tracker-specific branches."""

    def __init__(self) -> None:
        self._plugins: dict[str, TrackerPlugin] = {}
        self._external: list[ExternalPluginRecord] = []

    def register(self, plugin: TrackerPlugin) -> None:
        manifest = plugin.manifest
        self._validate_manifest(manifest)
        if manifest.id in self._plugins:
            raise PluginRegistrationError(f"Duplicate plugin ID: {manifest.id}")
        self._plugins[manifest.id] = plugin

    def plugins(self) -> tuple[TrackerPlugin, ...]:
        return tuple(self._plugins[key] for key in sorted(self._plugins))

    def manifests(self) -> tuple[TrackerManifest, ...]:
        return tuple(plugin.manifest for plugin in self.plugins())

    def get(self, plugin_id: str) -> TrackerPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as error:
            raise PluginNotFoundError() from error

    def resolve(self, url: str) -> tuple[TrackerPlugin, TrackerTarget]:
        matches = [plugin for plugin in self.plugins() if plugin.supports(url)]
        if len(matches) != 1:
            raise PluginNotFoundError()
        plugin = matches[0]
        canonical = plugin.normalize_url(url)
        if not plugin.supports(canonical):
            raise PluginRegistrationError("Plugin returned a URL outside its own support boundary.")
        return plugin, TrackerTarget(url, canonical, plugin.extract_external_id(canonical))

    def discover_external(self, plugins_dir: Path) -> tuple[ExternalPluginRecord, ...]:
        """Read manifests only: user-provided Python is never executed in Phase 3."""
        records: list[ExternalPluginRecord] = []
        known_ids = set(self._plugins)
        if plugins_dir.exists():
            for directory in sorted(path for path in plugins_dir.iterdir() if path.is_dir()):
                record = self._read_external_manifest(directory, known_ids)
                records.append(record)
                if record.manifest is not None:
                    known_ids.add(record.manifest.id)
        self._external = records
        return tuple(records)

    @property
    def external_plugins(self) -> tuple[ExternalPluginRecord, ...]:
        return tuple(self._external)

    def _read_external_manifest(self, directory: Path, known_ids: set[str]) -> ExternalPluginRecord:
        try:
            raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Manifest must be a JSON object.")
            manifest = self._manifest_from_mapping(raw)
            self._validate_manifest(manifest)
            if manifest.id in known_ids:
                raise ValueError(f"Duplicate plugin ID: {manifest.id}")
            return ExternalPluginRecord(directory, manifest, enabled=False)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return ExternalPluginRecord(directory, None, enabled=False, load_error=str(error))

    @staticmethod
    def _manifest_from_mapping(raw: dict[str, object]) -> TrackerManifest:
        def strings(name: str) -> tuple[str, ...]:
            value = raw.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"Manifest {name} must be a string list.")
            return tuple(value)

        for name in ("id", "display_name", "version", "core_min_version"):
            if not isinstance(raw.get(name), str):
                raise ValueError(f"Manifest {name} is required.")
        if not isinstance(raw.get("plugin_api_version"), int):
            raise ValueError("Manifest plugin_api_version is required.")
        return TrackerManifest(
            id=raw["id"],  # type: ignore[arg-type]
            display_name=raw["display_name"],  # type: ignore[arg-type]
            version=raw["version"],  # type: ignore[arg-type]
            plugin_api_version=raw["plugin_api_version"],  # type: ignore[arg-type]
            core_min_version=raw["core_min_version"],  # type: ignore[arg-type]
            domains=tuple(strings("domains")),
            auth_modes=tuple(AuthenticationMode(value) for value in strings("auth_modes")),
            capabilities=tuple(PluginCapability(value) for value in strings("capabilities")),
        )

    @staticmethod
    def _validate_manifest(manifest: TrackerManifest) -> None:
        if not _PLUGIN_ID.fullmatch(manifest.id):
            raise PluginRegistrationError("Plugin ID must be lowercase ASCII and stable.")
        if manifest.plugin_api_version != PLUGIN_API_VERSION:
            raise PluginRegistrationError("Plugin API version is incompatible.")
        if not manifest.domains or any(
            not domain or domain != domain.lower() for domain in manifest.domains
        ):
            raise PluginRegistrationError("Plugin must declare lowercase supported domains.")
