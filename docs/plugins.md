# Плагины tracker

## Граница Phase 3

Phase 3 предоставляет framework, а не реализацию реальных tracker. Согласно
нормативному плану, RuTracker будет добавлен в Phase 5, NNM-Club и Kinozal — в
Phase 7. До этого в продукте нет plugin, принимающего пользовательские URL.
Тестовый `FakePlugin` используется только офлайн в test harness.

## Контракт

Реализация удовлетворяет typed `TrackerPlugin` contract из
`torrwatch.trackers.types`. Она объявляет `TrackerManifest`, реализует
`supports`, `normalize_url`, `extract_external_id`, `test_auth`, `check` и
`download_torrent`. `TrackerTarget` несёт original/canonical URL и stable
external ID. `RemoteReleaseState` содержит title, external ID, canonical URL,
preliminary `version_key`, optional timestamp/download reference, auth flag и
санитизированные metadata.

`version_key` является только сигналом для будущей проверки. Он не доказывает
изменение torrent: download, bencode validation и infohash comparison относятся
к последующим фазам.

Ошибки plugin должны быть `TrackerPluginError` с одним из безопасных кодов:
`INVALID_TARGET`, `UNSUPPORTED_PAGE`, `AUTH_REQUIRED`, `AUTH_FAILED`,
`PLUGIN_PARSE_ERROR`, `TEMPORARY_NETWORK_ERROR`, `RATE_LIMITED` либо
`TRACKER_UNAVAILABLE`. Сообщения не содержат cookies, credentials или tokens.

## Context и безопасность

Plugin получает `TrackerPluginContext`, но не Database, SQLAlchemy session,
`SecretBox` или raw HTTPX client.

- `ctx.http` — `PluginHttpClient`, scoped view общего Phase 2 transport. Он
  принудительно применяет manifest domains, назначенные session namespace и
  proxy profile; plugin не может выбрать Direct, сменить proxy или ослабить
  SSRF allowlist.
- `ctx.state` — `PluginStateNamespace`: JSON-only non-secret state, изолированный
  по plugin ID и logical scope. Таблица `plugin_states` не предназначена для
  credentials/cookies.
- `ctx.secrets` — только scoped in-memory values, переданные application service
  будущей фазы. Он не предоставляет произвольного доступа к app secrets.

Ни plugin, ни parser не открывает SQLAlchemy session, не создаёт HTTP client,
не реализует retry/proxy fallback, не вызывает notification/client/shell.
Все tracker HTTP-запросы проходят shared transport с TLS, redirect/DNS SSRF
validation, encryption-backed session jars, redaction, retry и rate limits.

## Registry и external directories

`PluginRegistry` регистрирует implementations детерминированно по ID, проверяет
unique ID и plugin API version, а URL выбирает исключительно через `supports`.
В core нет tracker-specific conditional branches.

`/data/plugins/<name>/manifest.json` discovery читает только JSON manifest и
изолирует invalid/duplicate plugin как disabled record с load error. В Phase 3
никакой Python из `/data/plugins` не исполняется: uploaded code считается
untrusted. Future trusted extension loading потребует explicit administrator
enable и restart; automatic download и hot reload отсутствуют.

## Как добавить plugin в будущей фазе

1. Создать отдельный module с typed manifest и independent URL/parser logic.
2. Объявить exact lowercase tracker domains and reject unsupported pages.
3. Использовать только `ctx.http`; передавать minimal sanitised state.
4. Добавить minimal local sanitized fixtures: normal, login-required, blocked,
   not-found и parser regression state.
5. Добавить offline tests for canonicalization, IDs, parser errors, auth and
   transport policy. Live tests могут быть только explicit opt-in.

Плагины — trusted application code после их будущего explicit enable; это не
sandbox boundary. Не помещайте в fixtures реальные usernames, cookies, tokens,
tracker HTML dumps или персональные данные.
