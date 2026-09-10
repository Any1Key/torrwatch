# API

На Фазе 0 доступны только инфраструктурные endpoint-ы:

- `GET /health/live` — процесс web запущен;
- `GET /health/ready` — миграция, bootstrap и БД готовы;
- `GET /metrics` — совместимый с Prometheus текстовый bootstrap metric.

Версионированный REST API `/api/v1/` появится вместе с ресурсами в последующих
фазах. Browser login пока не является публичным API.

