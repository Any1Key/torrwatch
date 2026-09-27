# Operational HTTP endpoints

На Фазе 0 доступны только инфраструктурные endpoint-ы:

- `GET /health/live` — процесс web запущен;
- `GET /health/ready` — миграция, bootstrap и БД готовы;
- `GET /metrics` — совместимый с Prometheus текстовый bootstrap metric.

TorrWatch does not expose a public REST API or API tokens. Administration is
performed through authenticated server-rendered pages and CSRF-protected forms.
Long-running actions enqueue durable worker jobs and return without performing
external tracker or client requests in the browser request.
## Browser configuration routes

`GET/POST /configure/{clients|proxies|notifications|sessions}` открывает/сохраняет
форму; `/{id}` редактирует существующее подключение (кроме sessions).
`/torrents/new` и `/torrents/{id}/edit` используют `POST /configure/monitors[/id]`.
`GET /resolve-url` выполняет только локальное registry resolution.
`POST /torrents/{id}/check`, `/torrents/{id}/pause`, `/notifications/{id}/test`
требуют form CSRF. Check и notification test ставятся в существующие очереди.
Все маршруты требуют admin session; secrets не возвращаются.
# Административные browser actions

`POST /clients/{id}/test` (admin + form CSRF) ставит проверку подключения в
durable очередь; `GET /queues` показывает результат и другие фоновые работы.
`POST /deliveries/{id}/retry` повторяет неудачную доставку, не дублируя активную.
`GET /torrents/{id}/delete` — подтверждение; одноимённый POST требует `confirm=yes`.
`remove_from_client=on` разрешает удалить только текущую задачу клиента без данных.
`POST /torrents/{id}/check` с `Accept: application/json` возвращает 202 и
`{queued, message}` без redirect/flash; обычная форма сохраняет redirect fallback.
