# TorrWatch

TorrWatch is a self-hosted application for monitoring explicitly added torrent
release URLs and safely propagating genuine torrent changes to configured
torrent clients. Torrent search, media discovery and VPN management are not
part of this project.

## Phase 0 status

The bootstrap foundation provides a FastAPI web process, a separate worker
heartbeat process, SQLite migrations, initial administrator login, Docker
Compose and automated checks. Monitor management and tracker access begin in
later phases.

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
