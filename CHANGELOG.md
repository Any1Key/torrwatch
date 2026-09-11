# Changelog

All notable changes to TorrWatch are documented here.

## Unreleased

- Established the Phase 0 application foundation.
- Added the Phase 3 typed tracker-plugin framework, deterministic registry,
  scoped transport/context boundary, namespaced non-secret plugin state and
  metadata-only external manifest discovery.
- Added Phase 5 RuTracker topic monitoring: offline parser fixtures, shared
  transport/session/proxy integration, forced torrent verification, strict
  metainfo validation, atomic artifact storage and idempotent release history.
- Added Phase 6 encrypted qBittorrent/Transmission client adapters and durable
  delivery jobs with lease recovery, idempotent retry and add → verify →
  remove replacement that never deletes downloaded data.
