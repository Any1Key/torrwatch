# API

На Фазе 0 доступны только инфраструктурные endpoint-ы:

- `GET /health/live` — процесс web запущен;
- `GET /health/ready` — миграция, bootstrap и БД готовы;
- `GET /metrics` — совместимый с Prometheus текстовый bootstrap metric.

## Phase 9 REST API

FastAPI generates OpenAPI for the authenticated `/api/v1/` surface. Read
resources include `/monitors`, `/trackers`, `/proxies`, `/clients`,
`/notifications`, `/events` and `/system`; tracker accounts remain an empty
v1 placeholder until account configuration is introduced. `POST`/`PUT`
mutations require the session-bound `X-CSRF-Token` header.

`POST /api/v1/monitors/{id}/check` returns `202` after idempotently enqueueing
the durable monitor check. It never performs a tracker request. Secret-bearing
configuration is write-only and is absent from all read serialization.
