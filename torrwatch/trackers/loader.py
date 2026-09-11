"""Application entrypoint for deterministic tracker plugin discovery."""

from __future__ import annotations

from collections.abc import Iterable

from torrwatch.core.config import Settings
from torrwatch.trackers.registry import PluginRegistry
from torrwatch.trackers.types import TrackerPlugin


def load_plugin_registry(
    settings: Settings, builtins: Iterable[TrackerPlugin] = ()
) -> PluginRegistry:
    """Register trusted in-tree plugins, then safely discover external metadata.

    Phase 3 intentionally has no real in-tree tracker implementation: RuTracker
    and the other production plugins belong to their specified later phases.
    """
    registry = PluginRegistry()
    for plugin in builtins:
        registry.register(plugin)
    registry.discover_external(settings.resolved_plugins_dir)
    return registry
