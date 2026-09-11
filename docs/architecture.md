# Архитектура

На Фазе 0 TorrWatch состоит из одного Python-проекта и одного образа с двумя
независимыми процессами:

- `torrwatch-web` запускает FastAPI и browser-маршруты;
- `torrwatch-worker` записывает heartbeat и является основой будущего worker.

Оба процесса используют SQLite в `/data/torrwatch.db`. Доступ к SQLite
настраивается с WAL, foreign keys и busy timeout. Схема создаётся только
Alembic-миграциями. Будущие мониторинг, transport, plugins и delivery будут
добавлены по фазам спецификации, а не в web-request.

В Compose единственный владелец startup-миграции — `torrwatch-web`. Worker
запускается только после успешного `/health/ready` web и не запускает Alembic:
это исключает конкуренцию двух процессов за SQLite DDL при развёртывании или
перезапуске.

## Фаза 1: scheduler и jobs

`monitor_items` хранит следующий срок проверки, интервал (минимум 5 минут),
enabled/paused и историю состояния. Scheduler выбирает только enabled,
не-paused monitor с `next_check_at <= now`, затем выполняет `INSERT ... ON
CONFLICT DO NOTHING` с уникальным `active_key=monitor-check:<id>`.

Состояния: `PENDING → RUNNING → SUCCESS|FAILED_RETRYABLE|FAILED_PERMANENT`;
`FAILED_RETRYABLE → RUNNING`; истёкший `RUNNING` recovery переводит в
`FAILED_RETRYABLE`. Claim выполняется коротким conditional UPDATE по ID,
статусу и `next_attempt_at`; владеет job только worker, изменивший строку.
SQLite `BUSY`/`locked` при конкурирующем claim обрабатывается как проигранный
claim, остальные DB-ошибки не подавляются.

Lease job по умолчанию живёт 180 секунд, а владелец продлевает его каждые 60
секунд; оба значения задаются bootstrap-параметрами и renewal должен быть
короче lease. Handler выполняется вне DB-транзакции. Каждое продление —
отдельный conditional `UPDATE` по `id`, `RUNNING` и `worker_id`. Нулевой
`rowcount` означает потерю ownership: worker прекращает продление и никогда не
фиксирует от своего имени success/failure после завершения handler. Heartbeat
подтверждает liveness worker, но не заменяет lease конкретной job.

Recovery до claim находит только `RUNNING` с истёкшим lease, очищает owner и
делает работу немедленно retryable. При SIGTERM worker не claim-ит новые jobs;
уже начатому handler разрешено корректно завершиться с продолжением renewal.
Если процесс прекращает выполнение, продление прекращается, и job безопасно
восстанавливается после истечения lease. Retry delays: 1m, 5m, 15m, 1h, затем
3h. В Фазе 1 handler no-op: tracker/network кода нет.

## Фаза 2: transport и сетевая безопасность

Будущий plugin получает только `HttpTransport` и типизированные
`TransportRequest`/`TransportResponse`; raw HTTPX client не является plugin
API. Каждая tracker URL и каждый redirect проходят `TrackerUrlPolicy`: разрешены
только HTTP(S), запрещены embedded credentials и адреса, которые после DNS
резолвинга не являются global. Plugins следующей фазы смогут передавать allowlist
доменов. Это отдельная политика от административных endpoints будущих
qBittorrent/Transmission/proxy/anti-bot.

Proxy precedence: monitor override, tracker/account assignment, global default,
иначе Direct. Если выбран profile, его отказ fail-closed по умолчанию; только
явный fallback может вести к Direct или другому profile, циклы запрещены. HTTPX
uses `socks5h` для SOCKS5, поэтому DNS name resolution выполняется proxy.

`tracker_sessions` изолирован по logical namespace. Cookie jar сериализуется и
шифруется before persistence; expired entries игнорируются при load; user-agent
хранится рядом для будущих browser/anti-bot cookies. `SecretBox` использует
versioned Fernet authenticated-encryption envelope и 32-byte `/data/master.key`
outside SQLite. Missing/wrong key produces a clear safe error.

HTTP retry ограничен attempt-ами внутри одного job: transient network/timeouts,
429 (including Retry-After), and 5xx use exponential jitter backoff. Unsafe POST
не retry без explicit `safe_to_retry`. Это не Phase 1 persisted job retry.
Rate limiting in-process per hostname uses async cancellation-aware sleep. Future
anti-bot is only pluggable extension with explicit challenge classification.

Загрузка proxy/session state и сохранение обновлённых cookies происходят в
коротких DB-сессиях до и после сетевого запроса; соединение с tracker никогда
не удерживается внутри транзакции SQLite. TLS verification всегда включена и не
является настройкой tracker plugin.
