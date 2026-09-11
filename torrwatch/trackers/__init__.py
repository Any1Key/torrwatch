"""Tracker plugin framework; tracker-specific implementations live beside this package."""

from torrwatch.trackers.registry import PluginRegistry
from torrwatch.trackers.types import RemoteReleaseState, TrackerManifest

__all__ = ["PluginRegistry", "RemoteReleaseState", "TrackerManifest"]
