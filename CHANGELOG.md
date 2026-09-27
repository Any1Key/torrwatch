# Changelog

All notable changes to TorrWatch are documented here.

## Unreleased

- Добавлен одноразовый импорт зашифрованной сессии трекера из браузерного
  расширения TorrWatch Session Import; Cookie и User-Agent передаются только
  после явного подтверждения и проверяются по домену выбранного плагина.
- Added detailed rotating web/worker debug logs, combined diagnostic download,
  and administrator-controlled secret redaction with safe-by-default behavior.
- Reworked the administrative UX around actionable health: grouped navigation,
  mobile menu, persistent worker/attention status, torrent status cards,
  release/delivery details, per-torrent timeline, readable background operations,
  event filters, human-readable configuration choices and safer forms.
- Removed the public `/api/v1` surface and API-token requirement; TorrWatch is
  administered through the authenticated, CSRF-protected web interface.
- Established the Phase 0 application foundation.
- Added the Phase 3 typed tracker-plugin framework, deterministic registry,
  scoped transport/context boundary, namespaced non-secret plugin state and
  metadata-only external manifest discovery.
- Added Phase 5 RuTracker topic monitoring: offline parser fixtures, shared
  transport/session/proxy integration, forced torrent verification, strict
  metainfo validation, atomic artifact storage and idempotent release history.
- Added Phase 6 encrypted qBittorrent/Transmission client adapters and durable
  delivery jobs with lease recovery, idempotent retry and add → verify →
  remove replacement that never deletes downloaded data.
- Added Phase 7 NNM-Club and Kinozal built-in plugins with isolated URL/parser
  behavior, sanitized offline fixtures and reuse of the shared monitor,
  validation and delivery pipeline.
- Added Phase 8 Telegram/webhook notification foundations with encrypted
  configuration and durable retryable delivery state.
- Finalized Phase 10 release operations: safe diagnostics, durable manual-check
  CLI, encrypted-state-aware backup, plugin inventory and acceptance coverage.
## Unreleased — исправление интерфейса v1

- NNM-Club 1.0.1: исправлены Windows-1251, заголовок `a.maintitle` и
  `download.php?id=` с отдельным ID вложения; Cloudflare challenge больше не
  выдаётся за истёкшую сессию. Добавлены независимые offline-регрессии.
  Схема БД, маршрутизация и механизмы доставки не изменены.

- Добавлены onboarding, постоянная навигация, локальный адаптивный дизайн,
  понятные пустые состояния и формы существующих подключений.
- Browser setup связывает monitor с клиентом, прокси и зашифрованной сессией;
  секреты write-only. Check now и тест уведомления используют durable queues.
- Добавлены offline integration regression tests; схема БД не изменена.
# Исправления администрирования

- Проверка подключения клиента через durable worker, страница очередей и повтор доставки.
- Проверенные статусы tracker sessions вместо вывода по старым ошибкам.
- Подтверждение удаления темы с опциональным удалением текущей задачи клиента без файлов.
- Исправлены ответ «Проверить сейчас», вложенная форма настроек и регистр часового пояса.
- Миграция `20260915_0009`: payload фоновых операций и статус проверки сессии.
