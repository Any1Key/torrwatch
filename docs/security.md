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

