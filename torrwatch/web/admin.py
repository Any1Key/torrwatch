"""Server-rendered configuration controllers. No outbound I/O in page actions."""

from __future__ import annotations

import json
import io
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from torrwatch.api.routes import templates
from torrwatch.clients.types import TorrentClientError
from torrwatch.core.logging import read_debug_logs
from torrwatch.core.secrets import SecretBox, SecretKeyError
from torrwatch.core.security import csrf_token, hash_password, require_admin, require_csrf
from torrwatch.db.models import MonitorItem, ProxyProfile, SystemSetting, TorrentClient, User
from torrwatch.notifications.types import NotificationError
from torrwatch.services.admin import AdminService, AdminValidationError
from torrwatch.services.admin_jobs import enqueue_client_test
from torrwatch.services.configuration import ConfigurationService
from torrwatch.services.jobs import now_utc
from torrwatch.services.session_import import SessionImportService
from torrwatch.trackers.types import TrackerPluginError
from torrwatch.transport.cookies import SessionStore

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_admin)])
session_import_router = APIRouter(include_in_schema=False)
EXTENSION_DIR = Path(__file__).with_name("extension")
SECTIONS = {
    "clients": (
        "Торрент-клиенты",
        "Добавить торрент-клиент",
        "Подключите qBittorrent или Transmission для доставки обновлённых торрентов.",
    ),
    "sessions": (
        "Трекеры и сессии",
        "Импортировать сессию",
        "Войдите на трекер в браузере и импортируйте Cookie для доступа к раздачам.",
    ),
    "proxies": (
        "Прокси-профили",
        "Добавить прокси",
        "Настройте маршрут к трекеру. Без профиля используется прямое соединение.",
    ),
    "paths": (
        "Пути хранения",
        "Добавить путь",
        "Сохраните именованные каталоги, чтобы выбирать их при создании монитора.",
    ),
    "notifications": (
        "Уведомления",
        "Добавить уведомление",
        "Получайте сообщения через Telegram или webhook. Этот шаг необязателен.",
    ),
}


@router.get("/extension/download", response_class=StreamingResponse)
def download_extension() -> StreamingResponse:
    """Download the bundled browser extension as an unpacked-extension ZIP."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(EXTENSION_DIR.iterdir()):
            if path.is_file():
                bundle.write(path, arcname=path.name)
    archive.seek(0)
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="torrwatch-session-extension.zip"'},
    )


@router.get("/integrations", response_class=HTMLResponse)
def integrations(request: Request) -> HTMLResponse:
    """Show installed and planned external integrations."""
    return render(request, "integrations.html")


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request) -> HTMLResponse:
    """Show the built-in TorrWatch setup and operations guide."""
    return render(request, "help.html")
# key, label, input kind, help. Sensitive values are never echoed into forms.
FIELDS = {
    "paths": [
        ("name", "Название", "text", "Например: Сериалы"),
        ("path", "Путь", "text", "Абсолютный путь на стороне торрент-клиента."),
        ("enabled", "Включён", "checkbox", ""),
    ],
    "clients": [
        ("name", "Название", "text", "Например: домашний Transmission"),
        ("type", "Тип", "select:QBITTORRENT,TRANSMISSION", ""),
        ("url", "URL", "url", "Адрес Web API, включая порт. LAN-адреса допустимы."),
        ("username", "Имя пользователя", "text", ""),
        (
            "password",
            "Пароль",
            "password",
            "При редактировании оставьте пустым, чтобы сохранить пароль.",
        ),
        (
            "save_path",
            "Каталог загрузок",
            "text",
            "Путь на стороне торрент-клиента, необязательно.",
        ),
        ("category", "Категория", "text", "qBittorrent, необязательно."),
        ("tags", "Теги", "text", "qBittorrent, через запятую."),
        ("enabled", "Включён", "checkbox", ""),
    ],
    "proxies": [
        ("name", "Название", "text", ""),
        ("type", "Тип", "select:DIRECT,HTTP,SOCKS5", ""),
        ("host", "Хост", "text", "Только для HTTP / SOCKS5."),
        ("port", "Порт", "number", "1–65535"),
        ("username", "Имя пользователя", "text", "Необязательно."),
        ("password", "Пароль", "password", "Оставьте пустым для сохранения."),
        (
            "fallback",
            "При отказе прокси",
            "select:DISABLED,DIRECT,PROFILE",
            "DISABLED — остановить запрос. DIRECT — явно разрешить прямое соединение. PROFILE — сохранить ранее настроенную цепочку.",
        ),
        ("enabled", "Включён", "checkbox", ""),
    ],
    "notifications": [
        ("name", "Название", "text", ""),
        ("type", "Тип", "select:TELEGRAM,WEBHOOK", ""),
        (
            "bot_token",
            "Bot token",
            "password",
            "Telegram: токен от BotFather. Сохранённый токен не показывается.",
        ),
        (
            "chat_id",
            "Chat ID",
            "password",
            "Telegram: ID чата, группы или канала. При редактировании можно оставить пустым.",
        ),
        (
            "url",
            "Webhook URL",
            "password",
            "Webhook: настроенный HTTP(S) адрес получателя. Адрес хранится зашифрованным.",
        ),
        (
            "authorization",
            "Authorization",
            "password",
            "Webhook: например Bearer …, необязательно.",
        ),
        (
            "headers_json",
            "Дополнительные заголовки JSON",
            "password",
            'Webhook: объект {"X-API-Key":"…"}, необязательно.',
        ),
        (
            "events",
            "Типы событий",
            "text",
            "Через запятую: UPDATE_DETECTED, DELIVERY_SUCCESS, DELIVERY_FAILED, TRACKER_AUTH_FAILED, TRACKER_BROKEN, CLIENT_UNAVAILABLE, SYSTEM_ERROR.",
        ),
        ("enabled", "Включён", "checkbox", ""),
    ],
    "sessions": [
        ("plugin", "Трекер", "plugins", ""),
        (
            "cookie",
            "Cookie",
            "cookie",
            "Вставьте значение name=value; name2=value2 из браузера или используйте расширение. После успешного импорта через расширение поле можно оставить пустым.",
        ),
        (
            "user_agent",
            "User-Agent",
            "text",
            "Скопируйте User-Agent из того же запроса для согласованной сессии.",
        ),
    ],
    "monitors": [
        (
            "name",
            "Название (автоматически)",
            "text",
            "Будет взято из заголовка страницы трекера после первой проверки.",
        ),
        ("url", "URL раздачи", "url", "Конкретная тема или раздача поддерживаемого трекера."),
        (
            "interval_seconds",
            "Интервал, секунды",
            "number",
            "Минимум 300; по умолчанию 1800 (30 минут).",
        ),
        (
            "client",
            "Торрент-клиент",
            "clients",
            "Необязательно. Первый check нового монитора доставит baseline в клиент.",
        ),
        (
            "storage_path",
            "Путь хранения",
            "paths",
            "Необязательно. Выберите именованный путь для загрузки.",
        ),
        ("proxy", "Прокси", "proxies", "Необязательно; без профиля — Direct."),
        ("enabled", "Включён", "checkbox", ""),
        ("paused", "На паузе", "checkbox", ""),
    ],
}


def service(request: Request) -> ConfigurationService:
    return ConfigurationService(
        AdminService(request.app.state.database, request.app.state.plugin_registry),
        SecretBox(request.app.state.settings.resolved_master_key_file),
    )


def session_import_service(request: Request) -> SessionImportService:
    return SessionImportService(
        request.app.state.database,
        SecretBox(request.app.state.settings.resolved_master_key_file),
    )


@router.post("/session-import/start")
async def start_session_import(request: Request) -> JSONResponse:
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Некорректный запрос.")
    require_csrf(request, str(data.get("csrf", "")))
    plugin_id = str(data.get("plugin", "")).strip()
    try:
        plugin = request.app.state.plugin_registry.get(plugin_id)
    except TrackerPluginError as error:
        raise HTTPException(status_code=400, detail="Трекер не найден.") from error
    user = require_admin(request)
    token, expires_at = session_import_service(request).create_pairing(user.id, plugin_id)
    return JSONResponse(
        {
            "token": token,
            "plugin": plugin.manifest.id,
            "tracker": plugin.manifest.display_name,
            "expires_at": expires_at.isoformat(),
            "expires_in": 300,
        }
    )


@router.get("/session-import/current")
def current_session_import(request: Request, plugin: str) -> JSONResponse:
    try:
        tracker = request.app.state.plugin_registry.get(plugin)
        cookies, user_agent = SessionStore(
            request.app.state.database,
            SecretBox(request.app.state.settings.resolved_master_key_file),
        ).load(f"{tracker.manifest.id}:account:1")
    except (TrackerPluginError, SecretKeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Не удалось прочитать сессию.") from error
    cookie_header = "; ".join(f"{item.name}={item.value}" for item in cookies.jar)
    return JSONResponse(
        {
            "configured": bool(cookie_header),
            "cookie": cookie_header,
            "user_agent": user_agent or "",
        }
    )


@session_import_router.post("/session-import/complete")
async def complete_session_import(request: Request) -> JSONResponse:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid request payload.")
        token = str(data.get("token", "")).strip()
        domain = str(data.get("domain", "")).strip()
        user_agent_value = data.get("user_agent")
        user_agent = user_agent_value.strip() if isinstance(user_agent_value, str) else ""
        cookies = data.get("cookies")
        importer = session_import_service(request)
        plugin_id = importer.pairing_plugin_id(token)
        plugin = request.app.state.plugin_registry.get(plugin_id)
        if not isinstance(cookies, list):
            raise ValueError("No valid tracker cookies were supplied.")
        importer.complete_pairing(
            token,
            domain,
            plugin.manifest.domains,
            cookies,
            user_agent,
        )
    except (ValueError, KeyError, SecretKeyError, TrackerPluginError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(
        {
            "ok": True,
            "plugin": plugin_id,
            "message": "Сессия передана и сохранена. Запустите проверку трекера.",
        }
    )


def render(request: Request, template: str, **values: Any) -> HTMLResponse:
    status_code = values.pop("status_code", 200)
    active_path = request.url.path
    for section in (*SECTIONS, "monitors"):
        if active_path.startswith(f"/configure/{section}") or active_path == f"/settings/{section}":
            active_path = "/settings/sessions" if section == "sessions" else f"/{section}"
    if active_path == "/settings":
        active_path = "/system"
    health = AdminService(request.app.state.database, request.app.state.plugin_registry).dashboard()
    heartbeat = health.get("worker_heartbeat")
    worker_ok = False
    if heartbeat:
        from datetime import datetime

        worker_ok = (now_utc() - datetime.fromisoformat(heartbeat)).total_seconds() < 90
    attention = int(health.get("error_monitors", 0)) + int(health.get("pending_deliveries", 0))
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "csrf_token": csrf_token(request),
            "username": require_admin(request).username,
            "path": active_path,
            "flash": request.session.pop("flash", None),
            "timezone": _setting(request, "timezone") or "Europe/Moscow",
            "global_health": {
                "worker_ok": worker_ok,
                "attention": attention,
                "pending": health.get("pending_checks", 0),
            },
            **values,
        },
        status_code=status_code,
    )


def redirect(request: Request, url: str, message: str) -> RedirectResponse:
    request.session["flash"] = message
    return RedirectResponse(url, status_code=303)


@router.get("/")
@router.get("/dashboard")
def dashboard(request: Request) -> HTMLResponse:
    svc = service(request)
    return render(
        request,
        "dashboard.html",
        dashboard=svc.admin.dashboard(),
        sessions=svc.sessions(),
        channels=svc.admin.resource("notifications"),
        events=svc.admin.events()[:5],
    )


@router.get("/monitors")
def legacy_monitors(request: Request) -> RedirectResponse:
    return RedirectResponse("/torrents", status_code=303)


@router.get("/torrents")
def monitors(request: Request) -> HTMLResponse:
    svc = service(request)
    return render(request, "monitors.html", monitors=svc.admin.monitors())


@router.get("/trackers")
@router.get("/tracker-accounts")
def trackers(request: Request) -> RedirectResponse:
    return RedirectResponse("/settings/sessions", status_code=303)


@router.get("/paths")
@router.get("/clients")
@router.get("/proxies")
@router.get("/notifications")
def configuration_list(request: Request) -> HTMLResponse:
    return listing(request, request.url.path.strip("/"))


@router.get("/settings/{resource}")
def listing(request: Request, resource: str) -> HTMLResponse:
    if resource not in SECTIONS:
        raise HTTPException(404)
    svc = service(request)
    rows = svc.sessions() if resource == "sessions" else svc.admin.resource(resource)
    jobs = svc.admin.resource("notification-jobs") if resource == "notifications" else []
    return render(
        request,
        "configuration.html",
        resource=resource,
        section=SECTIONS[resource],
        rows=rows,
        jobs=jobs,
    )


def form(
    request: Request,
    resource: str,
    ident: int | None = None,
    values: dict[str, str] | None = None,
    error: str | None = None,
    error_field: str | None = None,
    selected_plugin: str | None = None,
) -> HTMLResponse:
    if resource not in FIELDS:
        raise HTTPException(404)
    svc = service(request)
    if values is None:
        values = {
            "enabled": "on",
            "interval_seconds": "1800",
            "session": "shared",
            "events": "UPDATE_DETECTED,DELIVERY_SUCCESS,DELIVERY_FAILED",
        }
        if resource == "sessions" and selected_plugin:
            values["plugin"] = selected_plugin
            try:
                tracker = request.app.state.plugin_registry.get(selected_plugin)
                cookies, user_agent = SessionStore(
                    request.app.state.database,
                    SecretBox(request.app.state.settings.resolved_master_key_file),
                ).load(f"{tracker.manifest.id}:account:1")
                values["cookie"] = "; ".join(
                    f"{item.name}={item.value}" for item in cookies.jar
                )
                values["user_agent"] = user_agent or ""
            except (TrackerPluginError, SecretKeyError, ValueError):
                values["cookie"] = ""
                values["user_agent"] = ""
        enabled_clients = [row for row in svc.admin.resource("clients") if row.get("enabled")]
        if len(enabled_clients) == 1:
            values["client"] = str(enabled_clients[0]["id"])
        if ident is not None:
            if resource == "monitors":
                with svc.db.session() as s:
                    row = s.get(MonitorItem, ident)
                    if row is None:
                        raise HTTPException(404)
                    values = {
                        "name": row.name,
                        "url": row.canonical_url,
                        "interval_seconds": str(row.check_interval_seconds),
                        "client": str(row.torrent_client_id or ""),
                        "proxy": str(row.proxy_override_id or ""),
                        "storage_path": next(
                            (
                                str(path["id"])
                                for path in svc.admin.resource("paths")
                                if path["path"] == row.client_save_path
                            ),
                            "",
                        ),
                        "enabled": "on" if row.enabled else "",
                        "paused": "on" if row.paused else "",
                    }
            else:
                row_dict = next((r for r in svc.admin.resource(resource) if r["id"] == ident), None)
                if row_dict is None:
                    raise HTTPException(404)
                values = {k: str(v) if v is not None else "" for k, v in row_dict.items()}
                values["enabled"] = "on" if row_dict["enabled"] else ""
                values["url"] = str(row_dict.get("base_url", ""))
                values["save_path"] = str(row_dict.get("default_save_path") or "")
                values["category"] = str(row_dict.get("default_category") or "")
                values["fallback"] = str(row_dict.get("fallback_mode", "DISABLED"))
                if resource == "notifications":
                    values["events"] = ",".join(json.loads(row_dict["event_types_json"]))
                if resource == "proxies":
                    with svc.db.session() as s:
                        proxy = s.get(ProxyProfile, ident)
                        if proxy:
                            values["username"] = proxy.username or ""
                if resource == "clients":
                    with svc.db.session() as s:
                        client = s.get(TorrentClient, ident)
                        if client:
                            values["username"] = client.username or ""
                            values["tags"] = ",".join(json.loads(client.default_tags_json or "[]"))
    safe = {
        key: values.get(key, "")
        for key, _, kind, _ in FIELDS[resource]
        if kind != "password"
    }
    return render(
        request,
        "setup_form.html",
        resource=resource,
        ident=ident,
        fields=FIELDS[resource],
        values=safe,
        error=error,
        error_field=error_field,
        plugins=svc.admin.trackers(),
        clients=svc.admin.resource("clients"),
        proxies=svc.admin.resource("proxies"),
        paths=svc.admin.resource("paths"),
        status_code=422 if error else 200,
    )


@router.get("/torrents/new")
def new_monitor(request: Request) -> HTMLResponse:
    return form(request, "monitors")


@router.get("/torrents/{ident}/edit")
def edit_monitor(request: Request, ident: int) -> HTMLResponse:
    return form(request, "monitors", ident)


@router.get("/configure/{resource}")
def new_configuration(request: Request, resource: str, plugin: str | None = None) -> HTMLResponse:
    return form(request, resource, selected_plugin=plugin)


@router.get("/configure/{resource}/{ident}")
def edit_configuration(request: Request, resource: str, ident: int) -> HTMLResponse:
    return form(request, resource, ident)


@router.post("/configure/{resource}", response_model=None)
@router.post("/configure/{resource}/{ident}", response_model=None)
async def save_configuration(
    request: Request, resource: str, ident: int | None = None
) -> HTMLResponse | RedirectResponse:
    if resource not in FIELDS:
        raise HTTPException(404)
    data = await request.form(max_fields=30)
    require_csrf(request, str(data.get("csrf", "")))
    values = {key: str(data.get(key, "")) for key, _, _, _ in FIELDS[resource]}
    try:
        svc = service(request)
        saved = (
            svc.save_monitor(values, ident)
            if resource == "monitors"
            else svc.save(resource, values, ident)
        )
    except (
        ValueError,
        KeyError,
        IntegrityError,
        TrackerPluginError,
        SecretKeyError,
        TorrentClientError,
        NotificationError,
    ) as exc:
        field = str(exc) if isinstance(exc, ValueError) else ""
        if isinstance(exc, (TrackerPluginError, TorrentClientError, NotificationError)):
            field = "plugin" if resource == "sessions" else "url"
        messages = {
            "name": "Укажите название длиной от 1 до 255 символов.",
            "cookie": "Вставьте значение Cookie в формате name=value; name2=value2.",
            "user_agent": "User-Agent должен быть одной строкой длиной до 1024 символов.",
            "interval_seconds": "Минимальный интервал — 300 секунд.",
            "url": "Укажите допустимый URL: конкретную раздачу либо HTTP(S) адрес сервиса.",
            "client": "Выберите существующий включённый торрент-клиент.",
            "proxy": "Выберите существующий включённый прокси.",
            "events": "Выберите типы событий из списка в подсказке.",
            "fallback": "PROFILE доступен только для ранее настроенной цепочки.",
        }
        message = messages.get(
            field, "Проверьте обязательные поля, URL, тип и уникальность названия."
        )
        return form(
            request,
            resource,
            ident,
            values,
            f"Не удалось сохранить. {message} Секретные поля введите заново.",
            field if field in messages else None,
        )
    target = f"/torrents/{saved}" if resource == "monitors" else f"/settings/{resource}"
    return redirect(
        request,
        target,
        "Сохранено. Секретные значения скрыты."
        if resource != "monitors"
        else "Монитор сохранён. Можно поставить проверку в очередь.",
    )


@router.get("/resolve-url")
def resolve_url(request: Request, url: str = "") -> dict[str, str | None]:
    try:
        plugin, target = request.app.state.plugin_registry.resolve(url)
        return {
            "tracker": plugin.manifest.display_name,
            "url": target.canonical_url,
            "id": target.external_id,
        }
    except TrackerPluginError:
        raise HTTPException(
            422, "Укажите URL конкретной раздачи поддерживаемого трекера."
        ) from None


@router.get("/torrents/{ident}")
def monitor_detail(request: Request, ident: int) -> HTMLResponse:
    svc = service(request)
    monitor = svc.admin.monitor(ident)
    if monitor is None:
        raise HTTPException(404)
    return render(
        request, "monitor_detail.html", monitor=monitor, timeline=svc.admin.timeline(ident)
    )


@router.post("/torrents/{ident}/check", response_model=None)
async def check(request: Request, ident: int) -> RedirectResponse | JSONResponse:
    require_csrf(request, str((await request.form()).get("csrf", "")))
    svc = service(request)
    if svc.admin.monitor(ident) is None:
        raise HTTPException(404)
    queued = svc.admin.force_check(ident)
    message = (
        "Проверка поставлена в очередь." if queued else "Проверка уже в очереди или выполняется."
    )
    if request.headers.get("accept") == "application/json":
        return JSONResponse({"queued": queued, "message": message}, status_code=202)
    return redirect(
        request,
        f"/torrents/{ident}",
        message,
    )


@router.post("/torrents/{ident}/pause")
async def pause(request: Request, ident: int) -> RedirectResponse:
    require_csrf(request, str((await request.form()).get("csrf", "")))
    with service(request).db.session() as s:
        item = s.get(MonitorItem, ident)
        if item is None:
            raise HTTPException(404)
        item.paused = not item.paused
    return redirect(
        request, "/torrents", "Расписание изменено. Уже запущенная проверка может завершиться."
    )


@router.get("/torrents/{ident}/delete")
def confirm_delete(request: Request, ident: int) -> HTMLResponse:
    monitor = service(request).admin.monitor(ident)
    if monitor is None:
        raise HTTPException(404)
    return render(request, "confirm_delete.html", monitor=monitor)


@router.post("/torrents/{ident}/delete")
async def delete_monitor(request: Request, ident: int) -> RedirectResponse:
    data = await request.form()
    require_csrf(request, str(data.get("csrf", "")))
    if data.get("confirm") != "yes":
        return redirect(request, f"/torrents/{ident}/delete", "Подтвердите удаление.")
    remove = data.get("remove_from_client") == "on"
    try:
        if not service(request).admin.delete_monitor(ident, remove_from_client=remove):
            raise HTTPException(404)
    except AdminValidationError as error:
        return redirect(request, f"/torrents/{ident}/delete", str(error))
    return redirect(
        request,
        "/torrents",
        "Торрент удалён из списка. "
        + (
            "Удаление задачи из клиента поставлено в очередь; файлы сохранятся."
            if remove
            else "Торрент в клиенте и его файлы оставлены без изменений."
        ),
    )


@router.post("/clients/{ident}/test")
async def test_client(request: Request, ident: int) -> RedirectResponse:
    require_csrf(request, str((await request.form()).get("csrf", "")))
    try:
        queued = enqueue_client_test(service(request).db, ident)
    except ValueError:
        raise HTTPException(422, "Клиент не найден или выключен.") from None
    return redirect(
        request,
        "/queues",
        "Проверка подключения поставлена в очередь."
        if queued
        else "Проверка подключения уже в очереди.",
    )


@router.get("/queues")
def queues(request: Request) -> HTMLResponse:
    admin = service(request).admin
    return render(
        request,
        "queues.html",
        monitors={row["id"]: row["name"] for row in admin.monitors()},
        clients={row["id"]: row["name"] for row in admin.resource("clients")},
        groups=[
            ("Проверки и операции клиента", "jobs", admin.resource("jobs")),
            ("Доставка торрентов", "deliveries", admin.resource("deliveries")),
            ("Уведомления", "notification-jobs", admin.resource("notification-jobs")),
        ],
    )


@router.post("/deliveries/{ident}/retry")
async def retry_delivery(request: Request, ident: int) -> RedirectResponse:
    data = await request.form()
    require_csrf(request, str(data.get("csrf", "")))
    if not service(request).admin.retry_delivery(ident):
        return redirect(
            request,
            "/queues",
            "Повтор недоступен: задача уже выполняется, торрент удалён или клиент выключен.",
        )
    return redirect(request, "/queues", "Повторная доставка поставлена в очередь.")


@router.post("/notifications/{ident}/test")
async def test_notification(request: Request, ident: int) -> RedirectResponse:
    require_csrf(request, str((await request.form()).get("csrf", "")))
    try:
        service(request).test_notification(ident)
    except ValueError:
        raise HTTPException(422, "Канал недоступен или выключен.") from None
    return redirect(
        request,
        "/notifications",
        "Тестовое уведомление поставлено в очередь. Результат показан в истории отправок.",
    )


@router.post("/system/password")
async def change_password(request: Request) -> RedirectResponse:
    data = await request.form()
    require_csrf(request, str(data.get("csrf", "")))
    password = str(data.get("password", ""))
    confirmation = str(data.get("password_confirmation", ""))
    if len(password) < 6:
        return redirect(request, "/system", "Пароль должен содержать минимум 6 символов.")
    if password != confirmation:
        return redirect(request, "/system", "Пароли не совпадают.")
    user = require_admin(request)
    with request.app.state.database.session() as session:
        row = session.get(User, user.id)
        if row is None:
            raise HTTPException(404)
        row.password_hash = hash_password(password)
    return redirect(request, "/system", "Пароль администратора изменён.")


@router.get("/system")
@router.get("/settings")
def system(request: Request) -> HTMLResponse:
    svc = service(request)
    state = svc.admin.system()
    with svc.db.session() as s:
        schema = s.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    heartbeat = state["dashboard"]["worker_heartbeat"]
    from datetime import datetime

    age = (
        int((now_utc() - datetime.fromisoformat(heartbeat)).total_seconds()) if heartbeat else None
    )
    return render(
        request,
        "system.html",
        state=state,
        schema=schema,
        age=age,
        web_ready=request.app.state.ready,
        notifications=svc.admin.resource("notification-jobs"),
        events=svc.admin.events()[:10],
        timezone=_canonical_timezone(_setting(request, "timezone")),
        debug_mode=_setting(request, "debug_mode") == "true",
        debug_redact_secrets=_setting(request, "debug_redact_secrets") != "false",
        passwordless_login=_setting(request, "passwordless_login") == "true",
    )


def _setting(request: Request, key: str) -> str | None:
    with service(request).db.session() as session:
            return session.scalar(select(SystemSetting.value).where(SystemSetting.key == key))


def _system_log_text(request: Request, svc: Any) -> str:
    """Build the same complete diagnostic text shown by the download endpoint."""
    redact_secrets = _setting(request, "debug_redact_secrets") != "false"
    lines: list[str] = []
    debug_log = read_debug_logs(request.app.state.settings.data_dir, redact_secrets)
    if debug_log:
        lines.extend(("=== APPLICATION DEBUG LOG ===", debug_log))
    lines.append("=== PERSISTED EVENTS ===")
    for event in svc.admin.events():
        lines.append(
            f"{event['created_at']} [{event['level']}] {event['code']}: {event['message']}"
        )
        details = event.get("details")
        if details:
            rendered = json.dumps(details, ensure_ascii=False, indent=2)
            lines.append(rendered)
    return "\n".join(lines) + "\n"


def _canonical_timezone(value: str | None) -> str:
    choices = ("Europe/Moscow", "UTC", "Europe/Berlin", "Asia/Almaty")
    raw = (value or "Europe/Moscow").strip()
    return next((choice for choice in choices if choice.lower() == raw.lower()), "Europe/Moscow")


@router.post("/system/settings")
async def system_settings(request: Request) -> RedirectResponse:
    data = await request.form()
    require_csrf(request, str(data.get("csrf", "")))
    from zoneinfo import ZoneInfo

    timezone = _canonical_timezone(str(data.get("timezone", "Europe/Moscow")))
    try:
        ZoneInfo(timezone)
    except Exception:
        raise HTTPException(422, "Недопустимый часовой пояс.") from None
    debug = data.get("debug_mode") == "on"
    redact_secrets = data.get("debug_redact_secrets") == "on"
    passwordless = data.get("passwordless_login") == "on"
    with request.app.state.database.session() as session:
        for key, value in (
            ("debug_mode", debug),
            ("debug_redact_secrets", redact_secrets),
            ("passwordless_login", passwordless),
            ("timezone", timezone),
        ):
            row = session.get(SystemSetting, key)
            if row is None:
                session.add(
                    SystemSetting(
                        key=key, value=timezone if key == "timezone" else str(value).lower()
                    )
                )
            else:
                row.value = value if key == "timezone" else str(value).lower()
    import logging

    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)
    return redirect(request, "/system", "Настройки системы сохранены.")


@router.get("/system/logs")
def logs(request: Request) -> PlainTextResponse:
    """Download application debug logs and persisted events."""
    svc = service(request)
    return PlainTextResponse(_system_log_text(request, svc))


@router.get("/logs", response_class=HTMLResponse)
def log_page(request: Request) -> HTMLResponse:
    """Dedicated operational log viewer; downloading remains /system/logs."""
    svc = service(request)
    return render(
        request,
        "logs.html",
        log_text=_system_log_text(request, svc),
        debug_mode=_setting(request, "debug_mode") == "true",
    )


@router.get("/events")
def events(
    request: Request, monitor_id: int | None = None, level: str = "", query: str = ""
) -> HTMLResponse:
    admin = service(request).admin
    return render(
        request,
        "events.html",
        events=admin.filtered_events(monitor_id=monitor_id, level=level, query=query),
        monitors=admin.monitors(),
        filters={"monitor_id": monitor_id, "level": level, "query": query},
    )
