# Развёртывание

## Docker Compose

Скопируйте `.env.example` в `.env`, задайте уникальный пароль администратора и
выполните `docker compose up --build -d`. Сервис web по умолчанию слушает
только `127.0.0.1:8080`; для внешнего доступа используйте HTTPS reverse proxy.

Compose использует `./data:/data`. В этом каталоге находятся SQLite, WAL-файлы
и master key. Делайте резервные копии каталога с остановленным приложением или
с согласованной SQLite backup-процедурой из будущей CLI. Потеря master key
сделает будущие зашифрованные секреты невосстановимыми.

`data/`, `.env`, Git metadata, локальные virtual environments и tool caches
исключены из Docker build context. Runtime state передаётся контейнерам только
через bind mount `./data:/data`, а не встраивается в образ.

При первом запуске entrypoint может назначить владельца только для смонтированного
`/data`, после чего web и worker выполняются непривилегированным пользователем.

Контейнер запускает приложение непривилегированным пользователем, не требует
Docker socket, host network, `NET_ADMIN` или `privileged` mode.

## Administrative UI (Phase 9)

The UI is server-rendered and has no Node/SPA build step. Publish it through
HTTPS in production and set `TORRWATCH_SESSION_HTTPS_ONLY=true` so the
administrator session cookie is Secure. Reverse proxies must preserve ordinary
form posts and the `X-CSRF-Token` header used by JSON mutations. Do not expose
the admin UI or `/api/v1` to an untrusted network without an access boundary.

## Torrent clients (Phase 6)

qBittorrent и Transmission — administrator-configured internal endpoints.
Допускаются HTTP(S) LAN addresses, но endpoint не передаётся из tracker URL и
не может быть overridden на отдельном delivery request. Пароли/токены хранятся
только encrypted в SQLite и требуют сохранённый `/data/master.key` для работы.
HTTPS clients проверяют certificate; не отключайте verification для локального
сертификата — установите доверенную CA в runtime image/configuration.

Delivery выполняется worker-ом из durable queue. При замене он добавляет и
проверяет новый validated torrent до удаления старого, передавая
`deleteFiles=false` (qBittorrent) или `delete-local-data=false` (Transmission).
Поэтому автоматический retry не удаляет download data.
