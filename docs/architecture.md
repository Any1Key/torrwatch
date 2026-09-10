# Архитектура

На Фазе 0 TorrWatch состоит из одного Python-проекта и одного образа с двумя
независимыми процессами:

- `torrwatch-web` запускает FastAPI и browser-маршруты;
- `torrwatch-worker` записывает heartbeat и является основой будущего worker.

Оба процесса используют SQLite в `/data/torrwatch.db`. Доступ к SQLite
настраивается с WAL, foreign keys и busy timeout. Схема создаётся только
Alembic-миграциями. Будущие мониторинг, transport, plugins и delivery будут
добавлены по фазам спецификации, а не в web-request.

