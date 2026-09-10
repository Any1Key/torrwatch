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
