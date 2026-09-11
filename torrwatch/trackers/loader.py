"""Application entrypoint for deterministic tracker plugin discovery."""

from __future__ import annotations

from collections.abc import Iterable

from torrwatch.core.config import Settings
from torrwatch.trackers.registry import PluginRegistry
from torrwatch.trackers.types import TrackerPlugin


def builtin_plugins() -> tuple[TrackerPlugin, ...]:
    """Return trusted in-tree implementations in deterministic registration order."""

    from torrwatch.trackers.rutracker import RuTrackerPlugin

    return (RuTrackerPlugin(),)


def load_plugin_registry(
    settings: Settings, builtins: Iterable[TrackerPlugin] | None = None
) -> PluginRegistry:
    """Register trusted in-tree plugins, then safely discover external metadata.

    Built-ins are trusted application code. External directories remain
    metadata-only and never execute user-provided Python.
    """
    registry = PluginRegistry()
    for plugin in builtin_plugins() if builtins is None else builtins:
        registry.register(plugin)
    registry.discover_external(settings.resolved_plugins_dir)
    return registry
