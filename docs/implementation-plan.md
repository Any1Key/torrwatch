# План реализации TorrWatch 1.0

## Принципы выполнения

Работа ведётся по фазам из `SPEC.md`: следующая фаза начинается только после
проверки применимых критериев предыдущей. Изменения делаются небольшими
логическими коммитами. Внешние трекеры, реальные учётные данные и живые
торрент-клиенты не требуются для разработки и основного CI.

## Фаза 0 — Bootstrap

### Результат

Получить запускаемый и проверяемый фундамент на Python 3.12+: единый образ с
независимыми процессами web и worker, миграциями SQLite, минимальной
аутентификацией администратора и базовыми health-проверками.

### План работ

1. Создать `pyproject.toml`, lock-файл и настройки Ruff, mypy, pytest,
   coverage и pre-commit.
2. Создать пакеты `torrwatch.core`, `torrwatch.db`, `torrwatch.api`,
   `torrwatch.web`, `torrwatch.worker` и точки входа web/worker.
3. Реализовать конфигурацию только системных параметров, структурированное
   безопасное логирование и инициализацию каталогов `/data`.
4. Добавить SQLAlchemy-модели начальной схемы (`users`, `system_settings`,
   `worker_state`), включение SQLite WAL, foreign keys и busy timeout.
5. Настроить Alembic и первую миграцию, пригодную для чистого развёртывания.
6. Реализовать bootstrap администратора и сессионную аутентификацию с
   Argon2id, HttpOnly/SameSite cookie, сроком действия сессии и CSRF-защитой
   для изменяющих состояние browser-маршрутов. На Фазе 0 не реализовывать
   API-токены и пользовательские CRUD-функции следующих фаз.
7. Добавить `/health/live`, `/health/ready`, минимальную страницу входа и
   защищённую стартовую страницу; worker должен писать heartbeat и корректно
   завершаться по SIGTERM.
8. Добавить Dockerfile с непривилегированным пользователем и Compose с
   сервисами `web` и `worker` из одного образа, томом `./data:/data` и
   публикацией web только на `127.0.0.1`.
9. Добавить `.env.example`, CI GitHub Actions и начальные документы:
   архитектура, развёртывание, API, безопасность, плагины, changelog и
   лицензия.
10. Добавить unit/integration тесты для конфигурации, SQLite-настроек,
    миграции, health-маршрутов, аутентификации/CSRF и heartbeat worker.

### Ожидаемые файлы

- `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`;
- `torrwatch/main.py`, `torrwatch/worker/main.py`, `torrwatch/core/*`,
  `torrwatch/db/*`, `torrwatch/api/*`, `torrwatch/web/*`;
- `alembic.ini`, `alembic/env.py`, `alembic/versions/*`;
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`;
- `.github/workflows/ci.yml`, `tests/unit/*`, `tests/integration/*`;
- обязательные документы из раздела 52 спецификации.

### Критерии завершения

- чистая БД создаётся только миграцией;
- web и worker — отдельные команды одного образа;
- live и ready health-проверки отражают ожидаемые состояния;
- пароль не хранится открытым, cookie и CSRF-защита тестируются;
- `ruff check .`, `ruff format --check .`, `mypy torrwatch`, `pytest` и
  покрытие не ниже 80% проходят;
- `docker compose config` и `docker build .` проходят;
- документация описывает локальный/Compose запуск и начальные ограничения.

## Фаза 1 — Core/domain/jobs

Создать доменные модели monitor, release и event, схему и миграции, очередь
работ и планировщик с устойчивыми lock/heartbeat/recovery. Добавить unit-тесты
переходов задач, идемпотентности и интеграционные тесты конкурентного worker.
Критерий: монитор никогда не исполняется параллельно, перезапуск worker не
теряет и не дублирует работу.

**Статус: выполнено.** Добавлены модели monitor/release/event/job, миграция
Phase 0 → Phase 1, SQLite conditional claim, active-key idempotency,
возобновляемые leases, worker identity/heartbeat, recovery и no-op handler без
сети.

## Фаза 2 — Transport/security

Реализовать общий HTTP-транспорт, классификацию ошибок, retry/rate-limit,
профили Direct/HTTP/SOCKS5, защищённую обработку URL/redirect/SSRF, cookie jars,
шифрование секретов и redaction. Покрыть fake HTTP-сервером, тестами SSRF,
redirect, прокси, шифрования и отсутствия секретов в диагностике.

**Статус: выполнено.** Добавлены application-owned async transport,
persistent proxy profiles и encrypted isolated sessions, SSRF/redirect policy,
bounded HTTP retries, rate limiting и recursive secret redaction. Пройдены
static checks, offline security/migration tests и Docker build/runtime/restart
smoke tests. Реальные tracker plugins не добавлялись.

## Фаза 3 — Plugin framework

Определить типизированный контракт плагинов, manifests, загрузчик built-in и
`/data/plugins`, контроль API-версии/дубликатов/ошибок, scoped `ctx` и
namespaced state. Добавить fake-плагин и офлайн-набор тестов. Критерий: сломанный
сторонний плагин не мешает запуску core.

**Статус: выполнено.** Добавлены typed framework и test harness: manifests,
registry, metadata-only external discovery, scoped context и namespaced
non-secret state. Пройдены static checks, offline/migration tests и Docker
build/runtime/restart smoke tests. Согласно `SPEC.md`, реальный RuTracker
относится к Фазе 5, а NNM-Club и Kinozal — к Фазе 7; tracker-specific parsers,
login flows, torrent download и monitor-check handler здесь намеренно не
реализуются.

## Фаза 4 — Torrent engine

Добавить точный bencode/metainfo parser, v1/v2 hashes, проверку структуры,
атомарное хранилище и retention release history. Использовать образцы с
известными hash и тесты повреждённых torrent/ошибок записи. Критерий: валидная
текущая версия не теряется ни при какой ошибке обновления.

**Статус: выполнено.** Добавлен независимый strict bencode/metainfo engine с
точными raw-info v1/v2 hash, SHA-256 payload и проверкой v1/v2 структуры.
`TorrentStore` сохраняет private artifacts атомарно с fsync/revalidation,
запрещает перезапись release ID и применяет protected retention (default 5).
Существующая Phase 1 таблица `release_versions` уже содержит историю и поля
метаданных, поэтому схема и Alembic migration не изменялись. Загрузка с
трекера, запись release history из job и delivery намеренно остаются Фазами
5–6.

## Фаза 5 — Первый реальный tracker

Реализовать RuTracker как отдельный plugin: URL, auth/cookie через core,
проверку состояния и загрузку torrent. Добавить только офлайн fixtures и
end-to-end цепочку URL → plugin → torrent → release без клиента.

**Статус: выполнено.** Built-in `rutracker` принимает только конкретный
`/forum/viewtopic.php?t=<positive-id>` URL и канонизирует его к HTTPS. Он
использует только scoped Phase 2 transport, encrypted cookie session и
manifest allowlist. Worker выполняет check через durable `MONITOR_CHECK` job:
первый успешный check создаёт baseline, изменение `version_key` либо
периодическая forced verification (24 часа по умолчанию, `0` отключает) ведут
к загрузке, strict Phase 4 validation, атомарному private storage и сравнению
точных infohash. Только новый infohash создаёт `release_versions`; изменение
HTML с тем же infohash не является обновлением. Fixtures и интеграционные
тесты полностью offline. Torrent-client delivery намеренно отсутствует.

## Фаза 6 — Torrent clients

Создать adapter API, qBittorrent и Transmission, delivery queue, retries и
безопасную замену add → verify → remove(delete_data=false). Тестировать fake
RPC/API. Критерий: offline client не препятствует хранению release и не вызывает
повторной загрузки torrent при retry.

## Фаза 7 — Остальные trackers

Добавить изолированные NNM-Club и Kinozal plugins с fixtures, детерминированной
классификацией auth/network/blocking и тем же transport. Критерий: изменения
парсеров ловятся offline тестами.

## Фаза 8 — Notifications

Добавить Telegram и generic webhook, события, зашифрованные секреты,
persisted bounded retries и fake endpoint-тесты. Критерий: сбой уведомления не
откатывает release или delivery.

## Фаза 9 — UI completion

Собрать серверные Jinja/HTMX страницы и REST `/api/v1` поверх общего service
layer: dashboard, monitors, timeline, plugins/accounts/proxies/clients,
notifications, events, settings, system status. Добавить accessibility и
browser/integration тесты для критических сценариев.

## Фаза 10 — Hardening/release

Провести security review, migration/backup/restore и fresh-deployment тесты,
Docker hardening, документационный аудит, acceptance suite, changelog и
release notes. Критерий: все v1 acceptance-тесты и quality gates из SPEC.md
проходят перед тегом `v1.0.0`.

## Зависимости фаз

`0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10`.

Транспорт и безопасность (Фаза 2) должны быть готовы до реальных плагинов;
плагинный контракт (Фаза 3) — до tracker-реализаций; torrent engine (Фаза 4) —
до загрузки реальных release; очередь и модели Фазы 1 — до delivery в Фазе 6.
