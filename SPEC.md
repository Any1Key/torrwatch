# TorrWatch 1.0 — Technical Specification

**Status:** normative specification v1.0  
**Working project name:** TorrWatch  
**Product type:** self-hosted web application  
**Primary deployment:** Docker Compose on Linux  
**Primary purpose:** monitor explicitly added torrent releases and safely propagate real torrent updates to a configured torrent client.

---

## 1. Product goal

TorrWatch monitors specific torrent releases explicitly added by URL.

Main flow:

1. The administrator adds a URL of an existing torrent release.
2. TorrWatch selects a compatible tracker plugin.
3. A background worker periodically checks the remote release.
4. If the remote state indicates a possible update, TorrWatch downloads the new `.torrent` file.
5. The torrent is parsed and validated as BitTorrent metainfo.
6. TorrWatch calculates the exact torrent hashes and metadata.
7. If the torrent really changed, a new release version is stored.
8. If a torrent client is configured, TorrWatch safely delivers the new torrent to it.
9. Notifications are sent according to the configured channels.
10. All checks, updates, deliveries and failures are recorded in an event history.

Core product statement:

> Add a specific torrent release → TorrWatch watches it → when the torrent actually changes, TorrWatch safely updates the configured torrent client.

---

## 2. Explicit non-goals

The following functionality is intentionally out of scope for TorrWatch 1.x:

- torrent search;
- movie/TV/game/content search;
- media library management;
- TMDB/IMDb/Kinopoisk metadata;
- Sonarr/Radarr/Prowlarr-like discovery;
- automatic search for alternative releases;
- release recommendations;
- release-uploader discovery;
- VPN client functionality;
- WireGuard/OpenVPN/AmneziaWG management;
- operating-system routing management;
- seed-ratio management;
- full torrent-client management UI;
- RSS aggregation as a product feature;
- embedded browser;
- CAPTCHA solving service;
- plugin marketplace;
- automatic installation of third-party plugin code from the Internet;
- Redis;
- RabbitMQ;
- Kafka;
- Elasticsearch;
- Kubernetes;
- microservice architecture;
- AI features;
- multi-tenant organizations or RBAC.

Do not add any of the above without an explicit specification change approved by the project owner.

---

## 3. Clean-room implementation

TorrWatch must be an independent implementation.

Allowed:

- study public tracker behavior;
- study public torrent-client APIs;
- study general architectural ideas from similar applications;
- use standards, RFCs and public library documentation.

Not allowed:

- copy source code from TorrentMonitor or another similar project;
- mechanically port tracker engines from another project;
- copy another project's UI, CSS, images or assets;
- copy code whose license has not been verified as compatible.

Built-in tracker support must be implemented independently.

---

## 4. Required technology stack

### Backend

- Python 3.12+;
- FastAPI;
- Pydantic;
- SQLAlchemy 2.x;
- Alembic;
- asyncio;
- HTTPX.

### Database

TorrWatch 1.0 requires SQLite only.

SQLite requirements:

- WAL mode;
- foreign keys enabled;
- appropriate busy timeout;
- explicit transaction boundaries for critical writes;
- no long network operations inside SQLite transactions.

The SQLAlchemy model should avoid unnecessary SQLite-specific assumptions so PostgreSQL can be added later if ever needed. PostgreSQL is not required in v1.0.

### Web UI

Do not build a separate SPA.

Use:

- FastAPI;
- Jinja2 server-side rendering;
- HTMX where useful;
- vanilla JavaScript;
- locally served CSS/assets.

The UI must work without CDN dependencies. A Node.js frontend build pipeline must not be introduced unless objectively necessary and documented in an ADR.

### Development tooling

- `pyproject.toml`;
- locked dependencies;
- Ruff;
- mypy;
- pytest;
- pytest-asyncio;
- coverage;
- pre-commit.

---

## 5. Runtime architecture

Use one source-code project and one application image, with at least two independent runtime processes:

```text
                ┌─────────────────┐
                │    Web / API    │
                └────────┬────────┘
                         │
                      SQLite
                         │
                ┌────────┴────────┐
                │     Worker      │
                └────────┬────────┘
                         │
           ┌─────────────┴─────────────┐
           │                           │
     Tracker plugins              Core services
           │                 HTTP / cookies / proxy
           └──────────────┬────────────┘
                          │
                       Internet
                          │
                   update detected
                          │
                Torrent-client adapters
                          │
                    Notifications
```

Web and Worker may use the same Docker image but must run as separate processes/services.

A web request must never execute a long monitor operation directly. It may enqueue a job and return immediately.

Do not launch monitoring via `exec`, `shell_exec`, detached web child processes, or equivalent hacks.

---

## 6. Repository structure

Preferred structure:

```text
torrwatch/
├── AGENTS.md
├── SPEC.md
├── README.md
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── dependency lock file
├── Dockerfile
├── docker-compose.yml
├── .env.example
│
├── torrwatch/
│   ├── main.py
│   ├── core/
│   ├── domain/
│   ├── db/
│   ├── transport/
│   ├── trackers/
│   ├── clients/
│   ├── notifications/
│   ├── torrent/
│   ├── worker/
│   ├── api/
│   ├── web/
│   └── cli/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── plugins/
│   └── fixtures/
│
└── docs/
    ├── architecture.md
    ├── plugins.md
    ├── api.md
    ├── deployment.md
    ├── security.md
    └── adr/
```

Small deviations are allowed if they materially improve maintainability. Significant architectural deviations require an ADR.

---

## 7. Tracker plugin architecture

Tracker plugins are the main extension point.

Core must not contain tracker-specific conditions such as:

```python
if tracker == "rutracker":
    ...
elif tracker == "nnmclub":
    ...
```

Tracker-specific parsing, URL conventions and tracker-specific authentication behavior belong in the plugin.

### 7.1 Plugin manifest

Each plugin must expose typed metadata equivalent to:

```json
{
  "id": "rutracker",
  "name": "RuTracker",
  "version": "1.0.0",
  "plugin_api_version": 1,
  "core_min_version": "1.0.0",
  "domains": ["rutracker.org"],
  "auth_modes": ["credentials", "cookie"],
  "capabilities": ["check", "download"]
}
```

### 7.2 Plugin interface

Conceptual interface:

```python
class TrackerPlugin:
    manifest: TrackerManifest

    def supports(self, url: str) -> bool: ...
    def normalize_url(self, url: str) -> str: ...
    def extract_external_id(self, url: str) -> str: ...

    async def test_auth(self, ctx) -> HealthResult: ...
    async def check(self, target, ctx) -> RemoteReleaseState: ...
    async def download_torrent(self, target, state, ctx) -> bytes: ...
```

Implement the actual contract as typed Protocols/ABCs in the project.

### 7.3 Plugin restrictions

A tracker plugin must not:

- access a SQLAlchemy session directly;
- write to the database directly;
- invoke notification channels;
- invoke torrent clients;
- instantiate an independent HTTP client;
- choose network/proxy policy on its own;
- persist credentials in files;
- implement its own generic retry engine;
- execute shell commands.

All network access must go through `ctx.http`.

Persistent plugin-specific non-secret state is accessed through a namespaced `ctx.state` API.

Plugin secrets are exposed only through a scoped `ctx.secrets` interface. A plugin must never see secrets belonging to another tracker/account.

### 7.4 Plugin loading

Support:

```text
built-in plugins
/data/plugins/*
```

Requirements:

- manifest required;
- plugin API version checked;
- unique plugin ID required;
- a broken third-party plugin must not prevent core startup;
- broken plugin shown as Broken/Disabled in UI;
- third-party plugin must be explicitly enabled by administrator;
- no hot reload required;
- installing/updating plugin requires restart;
- no automatic plugin downloads from the Internet.

Document clearly that third-party Python plugins are trusted code and run with the application's privileges. Sandboxing is not a v1 requirement.

---

## 8. Built-in tracker plugins for v1.0

Required built-in plugins:

### RuTracker

Support:

- topic URL recognition and normalization;
- release title/state retrieval;
- credentials authentication where practical;
- manual cookie authentication;
- session-cookie handling through core;
- proxy support through core;
- optional anti-bot transport through core;
- torrent download;
- deterministic error classification.

### NNM-Club

Support:

- release URL recognition and normalization;
- manual cookie authentication;
- credentials authentication only if technically reliable;
- proxy support;
- optional anti-bot fallback;
- torrent download;
- clear differentiation between expired authentication and network/blocking failures.

### Kinozal

Support:

- release URL recognition and normalization;
- credentials/session authentication;
- proxy support;
- update detection;
- torrent download.

Official mirrors/allowed hosts must be declared by the plugin, not hardcoded in core.

---

## 9. Unified HTTP transport

Every tracker request must use a shared transport layer.

The transport layer owns:

- request timeout;
- TLS verification;
- redirects;
- user-agent policy;
- cookies and session jars;
- proxy selection;
- retries;
- rate limiting;
- response metadata;
- error classification;
- optional anti-bot fallback;
- secret redaction.

The plugin owns only tracker-specific request semantics and response parsing.

A plugin may request method/URL/headers/body, but must not bypass transport policy.

---

## 10. Proxy support

Supported network modes only:

- Direct;
- HTTP proxy;
- SOCKS5 proxy.

No VPN functionality.

### 10.1 Proxy profile

Fields:

- id;
- name;
- type;
- host;
- port;
- optional username;
- optional encrypted password;
- enabled.

### 10.2 Assignment precedence

A proxy profile can be assigned globally, per tracker/account, and optionally per monitor.

Resolution order:

```text
monitor override
↓
tracker/account proxy
↓
global default
↓
Direct
```

### 10.3 Proxy fallback

Never silently bypass a configured proxy.

Fallback is explicit:

- Disabled (default);
- Direct;
- another proxy profile.

If fallback is Disabled and the proxy fails, the request must fail as a proxy/network error.

### 10.4 Proxy test

UI action `Test proxy` should report:

- DNS/connection result;
- authentication result;
- HTTP result;
- latency;
- ability to reach a selected tracker.

External-IP display is optional.

---

## 11. Cookie and session management

Core owns cookie/session persistence.

Requirements:

- isolated cookie jar per tracker account;
- encrypted persistent cookies;
- expiry handling;
- session refresh;
- manual cookie import;
- clear-session action;
- last successful authentication timestamp;
- no secrets in logs/events/API errors.

---

## 12. Optional anti-bot transport

An external FlareSolverr-compatible service may be supported as an optional integration.

Requirements:

- TorrWatch must function without it;
- it is not embedded in the web/worker process;
- it may be enabled by an optional Docker Compose profile;
- when technically supported, it must use the same selected proxy profile as the original tracker request;
- anti-bot results may update core-managed cookies/user-agent state;
- finite timeouts and no infinite retry loops.

Flow:

```text
regular request
↓
recognized challenge response
↓
anti-bot enabled?
↓ yes
external anti-bot request
↓
normalized response/session
```

If the challenge cannot be resolved, classify the tracker/account as blocked rather than retrying forever.

---

## 13. SSRF and URL security

Monitor URLs are untrusted input.

Requirements:

- only `http` and `https` schemes accepted;
- URL must match an installed plugin;
- plugin controls allowed hosts/mirrors;
- every redirect host must be validated again;
- `file`, `ftp`, `gopher` and other schemes rejected;
- tracker monitor feature must not provide arbitrary URL fetching;
- localhost/private/link-local addresses must not become reachable through malicious hostname tricks or redirects unless explicitly part of a tracker plugin's verified allowed endpoint set (normally they are not).

Administrative torrent-client/proxy endpoints are separate admin settings and are not treated as tracker monitor URLs.

---

## 14. Remote release state

A plugin returns a typed object similar to:

```python
RemoteReleaseState(
    title="...",
    version_key="...",
    source_updated_at=...,
    download_ref=...,
    metadata={...},
)
```

`version_key` is only a change hint. It may be a tracker timestamp, version string or page fingerprint.

The final source of truth for whether the torrent changed is the validated torrent infohash/metainfo.

---

## 15. Monitor check algorithm

```text
load monitor
↓
load plugin/account/network policy
↓
plugin.check()
↓
compare version_key
```

If unchanged, record successful check and finish.

If changed:

```text
plugin.download_torrent()
↓
parse and validate torrent
↓
calculate exact hashes
↓
compare with current release
```

If torrent hash is unchanged:

- record that remote metadata changed but torrent did not;
- do not modify the torrent client;
- update safe remote state as appropriate.

If torrent hash changed:

- atomically store the new release;
- create a delivery job if a torrent client is configured;
- create notification events.

### Forced verification

Support periodic forced torrent verification independent of `version_key` to protect against parser mistakes.

Default: once every 24 hours. Administrator may disable or change it.

A forced verification must not touch the torrent client unless the actual torrent hash changes.

---

## 16. Torrent parsing and validation

Do not validate a torrent by searching for the word `announce` or checking its file size.

Use real BitTorrent metainfo parsing.

Validate as applicable:

- valid bencode/metainfo structure;
- `info` dictionary exists;
- name/name.utf-8 handling;
- single-file/multi-file structure;
- piece length;
- v1 `pieces` correctness;
- v2 metadata when present;
- reject clearly malformed structures.

Extract/store when available:

- BitTorrent v1 infohash;
- BitTorrent v2 hash;
- SHA-256 of raw `.torrent` bytes;
- torrent name;
- total size;
- file count.

For v1, infohash must be calculated over the exact bencoded `info` dictionary semantics required by the BitTorrent specification. Do not accidentally hash a reserialized structure whose byte encoding could differ.

Tests must include sample torrents with known expected hashes.

---

## 17. Torrent storage

Storage layout:

```text
/data/torrents/<monitor-id>/<release-id>.torrent
```

File writing must be atomic:

```text
temporary file
↓
write
↓
flush/fsync as appropriate
↓
validate
↓
atomic rename
```

Never delete the current torrent before a replacement is safely written.

Keep release history.

Default retention: 5 torrent versions per monitor.

Never delete:

- current release;
- a release referenced by a pending delivery;
- data required to recover an incomplete update.

---

## 18. Torrent-client adapter API

Torrent-client behavior is accessed through an adapter interface, conceptually:

```python
class TorrentClientAdapter:
    async def test_connection(self): ...
    async def inspect(self, torrent_hash): ...
    async def add(self, torrent, options): ...
    async def remove(self, torrent_hash, delete_data=False): ...
    async def replace(self, old_hash, new_torrent, options): ...
```

Required v1 adapters:

### qBittorrent

- authentication;
- connection test;
- add torrent;
- inspect torrent;
- remove torrent without deleting data;
- category;
- tags where supported;
- save path;
- paused/running behavior;
- safe replacement.

### Transmission

- RPC session negotiation;
- authentication;
- connection test;
- add;
- inspect;
- remove with local data preserved;
- download directory;
- paused/running behavior;
- safe replacement.

TorrServer, Synology Download Station and Deluge are future work, not v1 requirements.

---

## 19. Safe torrent replacement

When a torrent infohash changes:

```text
inspect old torrent
↓
add NEW torrent
↓
verify new torrent was accepted
↓
remove OLD torrent with delete_data=false
```

Never remove the old torrent before the new torrent has been accepted.

If adding the new torrent fails:

- keep old torrent untouched;
- keep the new TorrWatch release stored;
- delivery becomes retryable;
- do not re-download the torrent from the tracker for delivery retries.

If new add succeeds but old removal fails:

- preserve data;
- record a partial-delivery/cleanup condition;
- permit cleanup retry;
- do not roll back the new release automatically.

---

## 20. Database-backed delivery queue

Do not use Redis or an external message broker.

Delivery job fields:

- id;
- release_id;
- client_id;
- status;
- attempts;
- next_attempt_at;
- created_at;
- completed_at;
- last_error.

Statuses:

- PENDING;
- RUNNING;
- SUCCESS;
- FAILED_RETRYABLE;
- FAILED_PERMANENT.

Use bounded exponential backoff for retryable failures.

---

## 21. Scheduler and worker behavior

Worker is long-running.

Defaults:

- monitor interval: 30 minutes;
- minimum user-configurable interval: 5 minutes;
- global concurrent checks: 4;
- per-tracker concurrent checks: 1;
- small random jitter to avoid synchronized bursts.

Requirements:

- same monitor must never be checked concurrently;
- job ownership/lock must be persisted, not only in memory;
- accidental duplicate worker processes must not corrupt state;
- full horizontal scaling is not a v1 goal;
- SIGTERM must be handled gracefully;
- worker heartbeat stored;
- abandoned RUNNING jobs detected after restart and recovered safely.

---

## 22. Retry and error classification

Suggested transient network backoff:

```text
1 minute
5 minutes
15 minutes
1 hour
3 hours
```

Bound retries and cap delay.

HTTP 429: honor `Retry-After` when valid.

Authentication failure:

- do not create login storms;
- classify account `AUTH_REQUIRED`;
- use a longer retry interval until credentials/session are fixed.

Parser failure:

- classify `PLUGIN_PARSE_ERROR`;
- do not download/replace torrent based on uncertain parsing.

Permanent invalid target:

- classify `INVALID_TARGET`;
- no automatic retries.

Other useful state/error codes include:

- HEALTHY;
- PAUSED;
- PROXY_ERROR;
- BLOCKED;
- PLUGIN_MISSING;
- PLUGIN_BROKEN;
- CLIENT_UNAVAILABLE;
- UPDATE_PENDING_DELIVERY.

---

## 23. Monitor item

Minimum fields:

- id;
- user-visible name;
- original URL;
- canonical URL;
- external tracker ID;
- plugin ID;
- tracker account ID;
- enabled/paused;
- check interval;
- optional proxy override;
- torrent client ID;
- client save path;
- category/tags;
- initial-sync mode;
- created_at;
- updated_at;
- last_check_at;
- last_success_at;
- next_check_at;
- last_update_at;
- current release ID;
- current hashes;
- current status;
- consecutive failures.

### Initial sync

Two explicit modes:

1. **Baseline only** — default. Download/validate/store current torrent but do not send it to the client.
2. **Baseline + delivery** — explicitly add the current torrent to the configured client.

---

## 24. Data model

Core tables should include:

- users;
- monitor_items;
- tracker_accounts;
- tracker_settings;
- proxy_profiles;
- plugin_states;
- release_versions;
- torrent_clients;
- delivery_jobs;
- notification_channels;
- notification_jobs or equivalent retry state;
- events;
- system_settings;
- worker_state/job locks as needed.

### release_versions minimum

- id;
- monitor_id;
- detected_at;
- version_key;
- source_updated_at;
- infohash_v1;
- infohash_v2;
- torrent_sha256;
- torrent_name;
- total_size;
- file_count;
- file_path;
- metadata_json.

### events minimum

- id;
- monitor_id nullable;
- tracker/plugin ID nullable;
- level;
- event_code;
- user-readable message;
- sanitized details JSON;
- created_at.

Store timestamps in UTC. Render them in the configured application timezone.

---

## 25. Event history and diagnostics

Each monitor has a readable timeline such as:

```text
10:00 Checked successfully
09:30 Checked successfully
09:00 Update detected
09:00 Torrent validated
09:00 Client delivery successful
08:30 HTTP 403
08:31 Anti-bot retry successful
```

Ordinary diagnosis must not require reading raw container logs.

Technical details may be expandable but secrets must already be redacted before persistence.

---

## 26. Tracker health

Provide a tracker/plugin health page showing at least:

- plugin name/version/API version;
- enabled/disabled/broken;
- tracker account;
- authentication status;
- cookie/session status;
- proxy profile;
- last successful request;
- last HTTP status where safe;
- last error;
- consecutive failures.

Provide a `Test` action that checks connectivity/authentication without changing monitor release state.

---

## 27. Dashboard

At minimum show:

- total monitors;
- active;
- paused;
- monitors in error;
- pending deliveries;
- updates in the last 24 hours;
- worker heartbeat;
- next scheduled check;
- torrent-client health summary;
- tracker health summary.

---

## 28. Required web pages

- Login;
- Dashboard;
- Monitors;
- Monitor details/timeline;
- Add/Edit monitor;
- Tracker plugins;
- Tracker accounts;
- Proxy profiles;
- Torrent clients;
- Notifications;
- Events;
- Settings;
- System status.

Responsive desktop/mobile layout required. Native mobile app is not required.

---

## 29. Browser application interface

TorrWatch 1.x is administered through the server-rendered web UI. A public or
versioned REST API is intentionally out of scope. Browser actions must use the
same application/service layer as background work and must enqueue long-running
operations rather than perform tracker or client requests in the HTTP request.

Operational health and metrics endpoints are not an administrative API.

---

## 30. Authentication and authorization

TorrWatch 1.0 is a single-administrator application.

Requirements:

- administrator username;
- Argon2id password hash;
- secure session cookies;
- HttpOnly;
- SameSite;
- Secure when served through HTTPS;
- session expiration;
- CSRF protection for state-changing browser actions.

No RBAC or multi-user system required.

---

## 31. Secret storage

Secrets include:

- tracker passwords;
- manual cookies;
- proxy passwords;
- torrent-client passwords;
- Telegram bot tokens;
- webhook secrets;
- persistent session material.

Persistent secrets must be encrypted at rest.

Master key:

- supplied through environment or generated during first boot;
- stored outside SQLite;
- file permissions restricted;
- never committed to Git;
- never printed in logs.

Use authenticated encryption from a mature cryptography library, e.g. AES-GCM. Do not invent custom cryptography.

---

## 32. Secret redaction

Logs, event details, exception responses and diagnostics must redact fields/headers such as:

- Authorization;
- Cookie;
- Set-Cookie;
- password;
- token;
- passkey;
- api_key;
- secret;
- proxy credentials.

Sensitive query parameters must also be sanitized before storage/logging.

---

## 33. Notifications

Required v1 channels:

### Telegram

- bot token;
- chat ID;
- enabled event types;
- test action.

### Generic webhook

- HTTP POST JSON;
- configured endpoint;
- optional secret/header authentication;
- test action.

Event types should include:

- UPDATE_DETECTED;
- DELIVERY_SUCCESS;
- DELIVERY_FAILED;
- TRACKER_AUTH_FAILED;
- TRACKER_BROKEN;
- CLIENT_UNAVAILABLE;
- SYSTEM_ERROR.

Notification failure must never roll back a successful torrent update. Notification retries should be persisted and bounded.

---

## 34. Logging

Application logs go to stdout/stderr.

Support human-readable and structured JSON logging. Production default: JSON.

Useful fields:

- timestamp;
- level;
- component;
- event;
- monitor_id;
- job_id;
- tracker/plugin ID;
- message.

Never log secrets.

---

## 35. Health and metrics

Endpoints:

- `/health/live` — process is alive;
- `/health/ready` — DB/migrations/directories are ready.

Worker heartbeat health is reported separately so a healthy web process cannot hide a dead worker.

Expose Prometheus-compatible `/metrics` without requiring Prometheus itself.

Minimum metrics:

- checks total/failures;
- update count;
- delivery success/failure;
- request/check duration;
- active monitors;
- pending deliveries;
- worker heartbeat age.

---

## 36. Docker and deployment security

Primary deployment is Docker Compose.

Required services:

- `web`;
- `worker`.

Both may use the same application image.

Persistent data:

```text
./data:/data
```

Optional Compose profile may provide an anti-bot service.

Container requirements:

- non-root application user;
- no `privileged: true`;
- no Docker socket mount;
- no NET_ADMIN;
- no host networking requirement;
- writable application state only under `/data` plus necessary temporary locations;
- prefer read-only root filesystem where practical.

Built-in TLS is not required. Production documentation should recommend Caddy/Nginx/Traefik reverse proxy.

Default Compose publishing should preferably bind only to localhost, e.g. `127.0.0.1:8080`, unless the administrator explicitly changes it.

---

## 37. Configuration

Environment variables are for bootstrap/system-level settings only. User-managed application settings live in DB.

`.env.example` must contain documented placeholders only, never real secrets.

Suggested bootstrap variables:

```text
TORRWATCH_DATA_DIR=/data
TORRWATCH_LOG_LEVEL=INFO
TORRWATCH_BIND=0.0.0.0
TORRWATCH_PORT=8080
TORRWATCH_MASTER_KEY_FILE=/data/master.key
```

---

## 38. Migrations

Use Alembic.

Requirements:

- fresh DB created via migrations;
- upgrades supported;
- migrations must not silently destroy user data;
- irreversible operations documented;
- release notes describe migration impact when relevant.

---

## 39. Backup and recovery

Provide CLI:

```text
torrwatch backup
torrwatch doctor
torrwatch plugins list
torrwatch check <monitor-id>
torrwatch admin reset-password
```

Backup should include:

- SQLite DB;
- torrent history;
- required application metadata.

Master key handling must be explicit. Documentation must warn that encrypted credentials cannot be recovered without the master key.

The normal backup command must not accidentally expose the master key in a broadly portable archive without explicit user intent.

---

## 40. UI error handling

User-facing errors must be understandable.

Bad:

```text
HTTPX.ConnectError(...)
```

Good:

```text
Could not connect to proxy 10.0.0.5:1080: connection refused.
```

Technical details can be available in an expandable section after redaction.

---

## 41. Testing strategy

### Unit tests

Cover:

- domain logic;
- torrent parsing/hashing;
- update orchestration;
- retry logic;
- proxy resolution;
- URL/SSRF validation;
- secret encryption/redaction;
- plugin loader;
- worker/job state transitions.

### Plugin tests

Every built-in tracker plugin must have offline fixtures such as:

```text
normal-page.html
updated-page.html
login-required.html
blocked-page.html
not-found.html
sample.torrent
```

Main CI must not depend on real torrent trackers.

Live tracker tests, if created, must be separate and opt-in.

### Integration tests

Use fake HTTP servers and fake torrent-client APIs.

Test the full chain:

```text
tracker response → plugin → torrent validation → release storage → delivery
```

### Security tests

At minimum:

- SSRF rejection;
- redirect host validation;
- CSRF;
- authentication;
- password hashing;
- secret redaction;
- encrypted secret round-trip.

### Coverage

CI minimum total threshold: 80%.

Critical modules such as torrent validation, security, secret storage and update orchestration require especially strong coverage.

Do not chase artificial 100% coverage.

---

## 42. CI

GitHub Actions on pull request and push:

```text
ruff
mypy
pytest
coverage threshold
docker build
docker compose config
```

Also add reasonable dependency/security scanning where practical.

Do not publish production images from ordinary pull-request builds.

---

## 43. Release process

Use Semantic Versioning.

For every release:

- Git tag;
- changelog;
- Docker image;
- migration notes when applicable;
- known issues.

Version `1.0.0` is cut only after all v1 acceptance tests pass.

---

## 44. Retention

Defaults:

- torrent releases: 5 per monitor;
- events: 90 days;
- technical diagnostics: 14 days where stored.

Configurable.

Never remove a current release or release required by pending work.

---

## 45. Privacy

No telemetry by default.

Do not transmit to project developers:

- tracker URLs;
- torrent names;
- hashes;
- credentials;
- usage data.

Any future telemetry must be explicit opt-in.

---

## 46. Plugin UI

Show:

- name;
- ID;
- version;
- plugin API version;
- declared domains;
- enabled state;
- health/load error.

Actions:

- Enable;
- Disable;
- Test.

Deleting plugin files from the UI is not required.

If a plugin disappears, dependent monitors remain in DB and move to `PLUGIN_MISSING`; they must not be deleted.

---

## 47. System status page

Show without secrets:

- TorrWatch version;
- Python version;
- DB schema version;
- web uptime;
- worker heartbeat;
- `/data` disk usage;
- loaded/broken plugins;
- pending deliveries;
- latest migration.

---

## 48. Add-monitor UX

Preferred flow:

```text
Paste URL
↓
Detect plugin
↓
Validate/canonicalize URL
↓
Select tracker account
↓
Select network/proxy profile
↓
Select torrent client
↓
Configure save path/category/tags
↓
Choose initial-sync mode
↓
Test access
↓
Save
```

If no plugin supports the URL, show a clear unsupported-tracker message.

---

## 49. Idempotency and transaction boundaries

Repeated handling of the same release must not:

- create duplicate release rows;
- re-add the same torrent endlessly;
- send duplicate success notifications indefinitely.

Use unique constraints/idempotency keys where appropriate.

Recommended update boundary:

```text
remote check
↓
remote torrent download
↓
torrent validation
↓
atomic file store
↓
short DB release transaction
↓
create delivery job
```

Torrent-client/network calls must not occur inside a long SQLite transaction.

---

## 50. Failure semantics

### Tracker updated, client offline

- release: STORED;
- delivery: PENDING/FAILED_RETRYABLE;
- monitor: UPDATE_PENDING_DELIVERY;
- after client recovery, delivery succeeds without re-downloading from tracker.

### Torrent invalid

- current release remains unchanged;
- invalid torrent must not be delivered;
- sanitized diagnostics recorded.

### Parser error

- do not infer an update;
- classify plugin parse error;
- preserve current state.

### Notification failure

- successful release/client state is not rolled back.

---

## 51. Accessibility and UX quality

Minimum:

- labels on controls;
- keyboard navigation;
- status not communicated only by color;
- sufficient contrast;
- responsive tables/forms;
- understandable empty states and error states.

---

## 52. Documentation requirements

Repository must contain:

- `README.md`;
- `docs/architecture.md`;
- `docs/deployment.md`;
- `docs/plugins.md`;
- `docs/security.md`;
- `docs/api.md`.

README must be sufficient for a clean installation without reading source code.

Significant architectural decisions must be recorded in `docs/adr/NNNN-title.md` containing:

- Context;
- Decision;
- Alternatives;
- Consequences.

---

## 53. Definition of Done for any feature

A feature is complete only when applicable items are satisfied:

- implementation complete;
- typed interfaces updated;
- migrations added if needed;
- tests added/updated;
- tests pass;
- Ruff passes;
- mypy passes;
- documentation updated;
- user-facing errors are clear;
- security impact considered;
- secrets are not exposed.

---

## 54. Development phases

### Phase 0 — Bootstrap

- repository/project structure;
- FastAPI web skeleton;
- SQLite + SQLAlchemy + Alembic;
- independent web and worker entrypoints;
- Dockerfile/Compose;
- basic admin authentication;
- CI;
- documentation skeleton.

### Phase 1 — Core/domain/jobs

- monitor model;
- release model;
- event model;
- scheduler/job persistence;
- worker heartbeat/locking/recovery.

### Phase 2 — Transport/security

- HTTP transport;
- cookies;
- retries;
- rate limiting;
- proxy profiles;
- secret store;
- SSRF/redirect protection.

### Phase 3 — Plugin framework

- plugin API;
- loader;
- manifest;
- namespaced plugin state;
- example/fake plugin;
- plugin test harness.

### Phase 4 — Torrent engine

- bencode/metainfo validation;
- exact hashes;
- atomic storage;
- release history/retention.

### Phase 5 — First real tracker

- RuTracker plugin;
- offline fixtures;
- end-to-end `URL → check → torrent → release` without torrent client.

### Phase 6 — Torrent clients

- qBittorrent;
- Transmission;
- delivery queue;
- safe replacement;
- fake integration tests.

### Phase 7 — Remaining trackers

- NNM-Club;
- Kinozal.

### Phase 8 — Notifications

- Telegram;
- webhook;
- persisted retries.

### Phase 9 — UI completion

- dashboard;
- monitor timeline;
- health pages;
- CRUD settings;
- system status.

### Phase 10 — Hardening/release

- security review;
- migration tests;
- Docker hardening;
- backup/restore tests;
- documentation review;
- acceptance test suite;
- release candidate.

Do not implement the whole project in one giant commit.

---

## 55. Acceptance tests for v1.0

Version 1.0 is not ready until all applicable tests pass.

### Fresh deployment

On a clean supported Linux host:

```bash
docker compose up -d
```

brings up a usable installation without manual DB editing.

### Persistence

After container restart/recreation, persistent state remains:

- monitors;
- encrypted credentials/session material;
- release history;
- torrent files;
- pending jobs.

### Baseline

Adding a supported tracker URL must:

- detect plugin;
- authenticate as configured;
- fetch remote state;
- download torrent when required;
- validate torrent;
- compute hashes;
- create baseline.

### Real update

Controlled fixture/integration update must:

- detect possible change;
- download and validate new torrent;
- recognize changed infohash;
- store release;
- create delivery;
- deliver safely;
- record event/notification.

### False-positive remote marker

If HTML/version marker changes but torrent hash is identical:

- no torrent-client change;
- no duplicate release representing a false content update.

### Client offline

If client is unavailable:

- release still stores successfully;
- delivery remains retryable;
- after client returns, delivery succeeds without re-fetching tracker torrent.

### Proxy failure

With explicit proxy and fallback Disabled:

- proxy failure is visible;
- direct tracker request is not attempted.

### Authentication expiry

Expired/invalid session transitions to a clear auth-required state rather than infinite retries.

### Parser regression

Changed HTML fixture causes deterministic plugin tests to fail.

### Restart during work

Worker restart must not cause duplicate torrent addition or corrupt job state.

### Secret safety

Real/semi-real test secrets must not appear in:

- stdout logs;
- event history;
- API responses;
- exception pages.

### Atomic storage

Simulated write failure must not destroy the current valid torrent release.

### SSRF

Monitor targets such as the following must be rejected unless impossibly and explicitly valid for an installed tracker plugin:

```text
http://127.0.0.1/
http://169.254.169.254/
file:///etc/passwd
```

---

## 56. Performance target

Target installation size:

- 1–500 monitored torrent releases;
- small self-hosted Linux server/VM;
- moderate resource use at idle;
- no optimization for millions of jobs.

A headless browser must not run inside the main application process.

---

## 57. Scope-control rule

When choosing between architectural solutions, prefer:

> simplicity over unnecessary universality

and:

> reliability over feature count

The AI/developer must not introduce torrent search, VPN, Redis, brokers, Kubernetes, SPA frameworks, media catalogs, recommendations or AI features without explicit owner approval.

---

## 58. Final v1 deliverable

Repository must contain:

- production source code;
- Dockerfile;
- Docker Compose configuration;
- `.env.example`;
- database migrations;
- built-in tracker plugins;
- qBittorrent adapter;
- Transmission adapter;
- Telegram notifications;
- webhook notifications;
- automated tests;
- CI;
- user/admin documentation;
- security documentation;
- backup/recovery procedure;
- upgrade procedure;
- changelog.

Before tagging `v1.0.0`, the project must show:

```text
ruff: PASS
mypy: PASS
pytest: PASS
coverage threshold: PASS
docker build: PASS
docker compose config: PASS
fresh deployment acceptance: PASS
```
