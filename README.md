# TorrWatch

TorrWatch is a self-hosted application for monitoring explicitly added torrent
release URLs and safely propagating genuine torrent changes to configured
torrent clients. Torrent search, media discovery and VPN management are not
part of this project.

## Current status

The foundation provides a FastAPI web process, a separate worker heartbeat
process, SQLite migrations, initial administrator login and Docker Compose.
Phase 2 adds the shared asynchronous tracker transport: encrypted isolated
cookie sessions, Direct/HTTP/SOCKS5 proxy profiles, SSRF and redirect
validation, bounded HTTP retry/rate limits and recursive secret redaction.
Phase 3 adds the typed tracker-plugin framework, registry, scoped context and
namespaced non-secret plugin state. Phase 4 adds strict local metainfo
validation with exact v1/v2 hashes and atomic, private torrent artifact storage
with protected release retention. Phase 5 adds the built-in RuTracker topic
plugin and durable URL → check → validated torrent → release-history flow.
RuTracker uses encrypted core cookie sessions (manual session import is the v1
method); it has no standalone credential store. Phase 6 adds encrypted,
administrator-configured qBittorrent and Transmission clients plus a durable
delivery queue. A real later infohash change queues delivery; an initial
baseline does not. Delivery always adds and verifies the new torrent before
removing the old one, and never requests data deletion. Phase 7 adds NNM-Club
and Kinozal specific-release plugins using the same encrypted sessions,
transport, metainfo validation and delivery pipeline. Search, crawling and
Phase 8 notifications remain out of scope.
Phase 8 adds encrypted Telegram and generic webhook notification channels with
durable bounded retries; notification failures never roll back releases or
torrent-client delivery.
Phase 9 adds an authenticated server-rendered administrative UI and versioned
REST API. Dashboard, monitors/timeline, configured integrations, events,
settings and system status use the same application service. “Check now” only
queues durable work; it never contacts a tracker in the HTTP request.

## Quick start with Docker Compose

1. Create a private runtime configuration: `cp .env.example .env`.
2. Replace `TORRWATCH_ADMIN_PASSWORD` with a unique password of at least
   12 characters. Do not commit `.env`.
3. Run `docker compose up --build`.
4. Open `http://127.0.0.1:8080/login` and sign in with the configured
   administrator name.

Persistent state is stored in `./data`. The application generates its master
key at `/data/master.key`; keep it with backups and never commit or disclose it.

See [deployment documentation](docs/deployment.md) for reverse-proxy and
backup notes. The full roadmap is in [the implementation plan](docs/implementation-plan.md).

## Operations and v1 limitations

`torrwatch doctor`, `torrwatch plugins list`, `torrwatch check <monitor-id>`,
`torrwatch backup <directory>` and `torrwatch admin reset-password` are the
supported operational commands. Backup excludes the master key unless
`--include-master-key` is explicit; without that key encrypted credentials and
sessions cannot be recovered. v1 has no search, crawling, VPN, media catalog or
CAPTCHA-bypass subsystem.
# Настройка через веб-интерфейс

После входа выполните шаги на странице обзора: **торрент-клиент → сессия
трекера → монитор → Проверить сейчас**. Уведомления необязательны.
Подробности и ограничения: [первичная настройка](docs/admin-setup.md).
