# Плагины tracker

## Встроенные plugins (Phase 5 и 7)

Phase 3 предоставляет framework. Phase 5 добавляет trusted built-in
`rutracker`; NNM-Club и Kinozal добавлены в Phase 7.
`rutracker` принимает только explicit topic URL
`https://rutracker.org/forum/viewtopic.php?t=<positive-id>` (вариант `www` и
HTTP нормализуются). Search, index, profile и произвольные forum pages
отвергаются. Тестовый `FakePlugin` остаётся только offline test harness.

`nnmclub` принимает только `https://nnmclub.to/forum/viewtopic.php?t=<positive-id>`;
`kinozal` — только `https://kinozal.tv/details.php?id=<positive-id>`. Их
download references остаются в manifest-declared hosts. Они не реализуют
search, crawl, account login flow или CAPTCHA bypass.

### NNM-Club 1.0.1: реальная forum-разметка

Плагин декодирует Windows-1251/UTF-8 по HTTP charset или HTML meta, без
замены повреждённых символов. Заголовок берётся из `a.maintitle`, чей URL
указывает на запрошенную тему. Ссылка `download.php?id=<attachment-id>`
содержит ID вложения, не ID темы. Поддержка прежней `topic-title`/`dl.php?t=`
разметки сохранена для регрессии. Неоднозначные ссылки отклоняются, одинаковые
дубли объединяются; из download reference исключаются session/query secrets.
HTTP/DNS/redirect/TLS policy по-прежнему принадлежит shared transport.

Предварительный `version_key` включает нормализованную ссылку на вложение,
название и доступные source markers. Если достоверной даты нет, timestamp
остаётся `None`. Изменение алгоритма может вызвать одну повторную проверку
торрента после обновления; только точный infohash решает, создавать ли release.
Прежняя периодическая forced verification обнаруживает изменения при том же ID.

Распознанный Cloudflare challenge классифицируется как `TRACKER_UNAVAILABLE`
с сообщением о блокировке, а не как доказательство истёкшей авторизации.
Отдельного нового состояния БД нет; применяется существующий bounded retry.
Плагин не запускает браузер/антибот-провайдер и не обходит challenge.
Страница входа по-прежнему даёт `AUTH_REQUIRED`.

Fixture `nnmclub/forum-topic.html` создана вручную по техническим признакам
одной разрешённой live-проверки, с синтетическими текстом и ID. Тесты передают
её в плагин как CP1251 bytes и проверяют общий baseline/change pipeline offline.

Live acceptance 2026-09-13: один разрешённый запрос через Direct с сохранённой
encrypted сессией дал HTTP 200; plugin.check вернул корректную кириллицу,
download reference и version key. Torrent download/delivery в этой диагностике
не выполнялись; состояние монитора и cookies не перезаписывались. Проверки:
140 offline tests passed, coverage 81.19%, Ruff/format/mypy PASS, Docker build PASS.

## Контракт

Реализация удовлетворяет typed `TrackerPlugin` contract из
`torrwatch.trackers.types`. Она объявляет `TrackerManifest`, реализует
`supports`, `normalize_url`, `extract_external_id`, `test_auth`, `check` и
`download_torrent`. `TrackerTarget` несёт original/canonical URL и stable
external ID. `RemoteReleaseState` содержит title, external ID, canonical URL,
preliminary `version_key`, optional timestamp/download reference, auth flag и
санитизированные metadata.

`version_key` является только сигналом для будущей проверки. Он не доказывает
изменение torrent: `version_key` является лишь preliminary сигналом. Для
RuTracker Phase 5 download выполняется только при initial baseline, изменённом
сигнале или forced verification; strict metainfo validation и точное сравнение
infohash решают, создавать ли новую историю.

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

## Sessions и fixtures

Phase 5/7 plugins используют существующий encrypted `tracker_sessions` через logical
`rutracker:monitor:<id>` namespace (или account namespace, когда оно назначено).
V1 поддерживает manual cookie/session import; нет отдельной таблицы plaintext
credentials и нет login storm. Login page классифицируется как `AUTH_REQUIRED`,
а unexpected markup — как `PLUGIN_PARSE_ERROR`.

Минимальные fixtures в `tests/fixtures/rutracker/` созданы вручную и содержат
только структуру, нужную parser tests. Они не содержат реальной страницы,
cookies, account names, tokens или other personal data. Normal CI никогда не
обращается к RuTracker.

Fixtures NNM-Club и Kinozal в `tests/fixtures/nnmclub/` и
`tests/fixtures/kinozal/` также hand-written/minimal: normal topic, login page
и broken markup. Они не являются dumps живых tracker pages.

## Clean-room parser boundary

Each parser independently extracts only title, stable ID, preliminary
marker/timestamp and one torrent reference. Missing/ambiguous markup is
`PLUGIN_PARSE_ERROR`; login document is `AUTH_REQUIRED`; raw document and
authentication material never enter error/event. Normal tests use fake scoped
HTTP and do not contact real trackers.

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
