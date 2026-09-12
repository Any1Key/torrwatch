# Безопасность

Пароль администратора передаётся только через bootstrap environment variable и
сохраняется в SQLite исключительно как Argon2id hash. Сессии используют
HttpOnly, SameSite=Lax cookie и имеют ограниченный срок жизни. Для всех
изменяющих browser-запросов применяется CSRF token.

Установите `TORRWATCH_SESSION_HTTPS_ONLY=true`, когда приложение обслуживается
через HTTPS reverse proxy. Master key создаётся отдельным файлом `/data/master.key`
с режимом `0600`; он не находится в SQLite и не должен попадать в логи, Git или
непреднамеренно переносимые архивы.

Шифрование прикладных секретов, redaction, SSRF-защита и transport policy
относятся к Фазе 2.

## Phase 2 transport policy

`SecretBox` uses maintained `cryptography` Fernet AEAD; versioned ciphertext is
in SQLite, while exact 32-byte key remains in `/data/master.key` mode `0600`.
Missing, invalid or changed key fails safely. Central redaction recursively
removes authorization, proxy authorization, cookies, passwords, tokens, keys,
passkeys, secrets and sensitive query parameters. Tracker URL policy validates
scheme, authority, hostname and DNS addresses initially and after redirects;
non-global addresses are forbidden, including localhost, loopback and
link-local targets. TLS certificate verification remains mandatory. Proxy
failure is fail-closed unless the administrator has configured an explicit,
acyclic fallback. Administrator-configured internal services are deliberately
governed by a separate policy.

## Phase 3 plugin boundary

Plugins receive a scoped transport view rather than raw HTTPX/`HttpTransport`:
application services bind its declared domain allowlist, session namespace and
selected proxy profile. Plugin code cannot silently switch to Direct or read
cookies/credentials from another account. Persistent `plugin_states` stores
only JSON non-secret state; encrypted cookies remain in `tracker_sessions`.

External directory discovery reads manifest JSON only in Phase 3 and never
executes user-provided Python. Invalid manifests are recorded as disabled;
automatic plugin downloads and hot reload are not supported. Fixture HTML is
minimal, local and sanitized; real credentials, cookies, tokens and personal
data are forbidden.

## Phase 5 RuTracker boundary

RuTracker-specific parser code receives only the already-scoped plugin context.
It never opens a database session, creates HTTPX clients, selects a proxy or
reads the encryption key. Its declared `rutracker.org` manifest domain becomes
the per-request transport allowlist, so normal Phase 2 DNS and redirect SSRF
validation remains in force for both topic and `.torrent` URLs. A configured
proxy remains fail-closed; plugin code has no Direct fallback API.

Cookies remain encrypted in `tracker_sessions`, isolated by `rutracker` plus
logical monitor/account namespace and paired with the saved User-Agent. Phase 5
uses manual encrypted cookie/session import rather than storing a RuTracker
password. Parser errors, auth states, events and jobs use short sanitized text;
they do not retain HTML bodies, cookies, download tokens or credentials.

Only strictly validated metainfo is atomically written to the private torrent
directory. A malformed response cannot replace the prior valid current release.

## Phase 4 torrent artifacts

Metainfo является недоверенным бинарным входом. Torrent engine использует
строгий bounded bencode parser, отклоняет trailing/noncanonical values,
небезопасные path components и несогласованную структуру v1/v2. Infohash
вычисляется из исходного raw диапазона `info`, а не из повторно сериализованного
словаря. Torrent payload и извлечённые метаданные не содержат учётных данных и
не логируются как диагностический дамп.

Файлы release имеют private permissions: `/data/torrents` и monitor namespaces
`0700`, каждый artifact `0600`. Storage сначала fsync-ит временный файл,
валидирует сохранённые байты и только затем делает atomic rename; существующий
release ID нельзя перезаписать другим payload. Retention получает protected
current/delivery IDs от orchestration и никогда не удаляет их. Ошибка записи,
валидации или cleanup оставляет прежний валидный artifact на месте.

## Phase 6 torrent-client boundary

Учетные данные qBittorrent/Transmission шифруются тем же versioned Fernet
`SecretBox`, что proxy и tracker sessions; plaintext не записывается в SQLite и
расшифровывается лишь при создании adapter для execution. Ошибка missing/wrong
master key прекращает доступ безопасно. API и diagnostics возвращают только
sanitized classifications, без password, Authorization, cookie или torrent
bytes.

Клиентские endpoints являются явно сохранённой административной конфигурацией:
поддерживаются только HTTP(S), embedded credentials/query/fragment запрещены,
redirects не следуются, TLS certificate verification обязательна. LAN/private
адреса допустимы здесь намеренно и не ослабляют tracker SSRF policy. Автоматическая
замена вызывает qBittorrent с `deleteFiles=false` и Transmission с
`delete-local-data=false`; данные пользователя не удаляются даже при retry.

## Phase 7 tracker plugins

NNM-Club и Kinozal получают только scoped `PluginContext`. Их domains declared
в manifest становятся transport allowlist; redirect и DNS revalidation,
encrypted isolated cookie sessions, selected proxy fail-closed behavior, TLS,
redaction, retry и rate limiting остаются общими Phase 2 controls. Manual
cookie import uses the same `tracker_sessions` namespaces; plugins не создают
plaintext credential storage и не сохраняют raw HTML. Local parser fixtures
ручно минимизированы и не содержат account data, cookies или tokens.

## Phase 8 notifications

Telegram bot token, webhook Authorization и static sensitive headers находятся
только в Fernet-encrypted channel config. Adapters never put these fields in
payload/error messages; webhook payload is versioned and contains sanitized
application state only. Webhook endpoints are administrator configured HTTP(S)
destinations with TLS verification and redirects disabled.

## Phase 9 web/API boundary

Administrative pages and `/api/v1` require the existing single-admin session.
The cookie remains HttpOnly and SameSite=Lax, and is Secure when HTTPS mode is
configured. Form mutations carry the session CSRF token; JSON mutations require
`X-CSRF-Token`. Templates and API read models omit encrypted configuration,
passwords, tokens, cookies and Authorization values.

Manual commands are not arbitrary URL fetchers. They enqueue existing durable
work after short transactions, so no request keeps a SQLite transaction open
across tracker/client/notification network I/O. Validation errors are safe
short messages and never render raw tracker HTML or exception traces.
## Browser configuration v1

Все configuration POST требуют admin session и CSRF. Secret inputs никогда
не получают `value`, включая повторный показ формы после ошибки. Cookie import
использует общий encrypted SessionStore; клиентские и proxy passwords —
SecretBox. Namespace общей UI-сессии `<plugin>:account:1` изолирован по plugin.
Сохранение cookie не означает подтверждённую авторизацию. Тест уведомления
enqueue-only; ошибки формы не показывают исключения и сторонние ответы.
