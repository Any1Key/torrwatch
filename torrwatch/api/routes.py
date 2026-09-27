"""Authenticated browser pages and operational endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from torrwatch.core.security import (
    SESSION_EXPIRES_KEY,
    USER_SESSION_KEY,
    csrf_token,
    require_admin,
    require_csrf,
    verify_password,
)
from torrwatch.db.models import SystemSetting, User
from torrwatch.services.admin import AdminService, AdminValidationError

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "web" / "templates")
)


def pretty_datetime(value: str | None, timezone: str = "Europe/Moscow") -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value).astimezone(ZoneInfo(timezone))
        return parsed.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


templates.env.filters["pretty_datetime"] = pretty_datetime


def filesize(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} Б"


templates.env.filters["filesize"] = filesize


def admin_service(request: Request) -> AdminService:
    return AdminService(request.app.state.database, request.app.state.plugin_registry)


def page_context(request: Request, user: User, **values: Any) -> dict[str, Any]:
    return {"csrf_token": csrf_token(request), "username": user.username, **values}


@router.get("/health/live", include_in_schema=False)
def live() -> JSONResponse:
    return JSONResponse({"status": "live"})


@router.get("/health/ready", include_in_schema=False)
def ready(request: Request) -> JSONResponse:
    if not request.app.state.ready:
        raise HTTPException(status_code=503, detail="Application is not ready.")
    try:
        with request.app.state.database.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as error:
        raise HTTPException(status_code=503, detail="Database is unavailable.") from error
    return JSONResponse({"status": "ready"})


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(
        "# HELP torrwatch_phase0_info TorrWatch bootstrap information\n"
        "# TYPE torrwatch_phase0_info gauge\ntorrwatch_phase0_info 1\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token(request), "error": None},
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login(
    request: Request,
    username: str = Form(),
    password: str = Form(""),
    csrf: str = Form(),
    remember: bool = Form(False),
) -> Response:
    require_csrf(request, csrf)
    with request.app.state.database.session() as database_session:
        user = database_session.query(User).filter(User.username == username).one_or_none()
        with request.app.state.database.session() as setting_session:
            passwordless = (
                setting_session.scalar(
                    select(SystemSetting.value).where(SystemSetting.key == "passwordless_login")
                )
                == "true"
            )
        local = request.client is not None and request.client.host in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        valid_password = (
            passwordless
            and local
            or (
                bool(password)
                and user is not None
                and verify_password(user.password_hash, password)
            )
        )
        if user is None or user.disabled or not valid_password:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf_token": csrf_token(request), "error": "Неверное имя или пароль."},
                status_code=401,
            )
        request.session[USER_SESSION_KEY] = user.id
        lifetime = timedelta(days=30) if remember else timedelta(hours=12)
        request.session[SESSION_EXPIRES_KEY] = (datetime.now(UTC) + lifetime).isoformat()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", include_in_schema=False)
def logout(
    request: Request, csrf: str = Form(), _: User = Depends(require_admin)
) -> RedirectResponse:
    require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=page_context(request, user, dashboard=admin_service(request).dashboard()),
    )


@router.get("/monitors", response_class=HTMLResponse, include_in_schema=False)
def monitors_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="monitors.html",
        context=page_context(request, user, monitors=admin_service(request).monitors()),
    )


@router.get("/monitors/new", response_class=HTMLResponse, include_in_schema=False)
def new_monitor_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="monitor_form.html", context=page_context(request, user, error=None)
    )


@router.post("/monitors", include_in_schema=False)
def create_monitor_page(
    request: Request,
    name: str = Form(""),
    url: str = Form(),
    interval_seconds: int = Form(1800),
    csrf: str = Form(),
    user: User = Depends(require_admin),
) -> Response:
    require_csrf(request, csrf)
    try:
        monitor = admin_service(request).create_monitor(name, url, interval_seconds)
    except AdminValidationError as error:
        return templates.TemplateResponse(
            request=request,
            name="monitor_form.html",
            context=page_context(request, user, error=str(error)),
            status_code=422,
        )
    return RedirectResponse(f"/monitors/{monitor['id']}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/monitors/{monitor_id}", response_class=HTMLResponse, include_in_schema=False)
def monitor_page(
    monitor_id: int, request: Request, user: User = Depends(require_admin)
) -> HTMLResponse:
    service = admin_service(request)
    monitor = service.monitor(monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found.")
    return templates.TemplateResponse(
        request=request,
        name="monitor_detail.html",
        context=page_context(request, user, monitor=monitor, timeline=service.timeline(monitor_id)),
    )


@router.post("/monitors/{monitor_id}/check", include_in_schema=False)
def check_monitor_page(
    monitor_id: int, request: Request, csrf: str = Form(), _: User = Depends(require_admin)
) -> RedirectResponse:
    require_csrf(request, csrf)
    try:
        admin_service(request).enqueue_check(monitor_id)
    except AdminValidationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=status.HTTP_303_SEE_OTHER)


_PAGES = {
    "trackers": "Tracker plugins",
    "tracker-accounts": "Tracker accounts",
    "proxies": "Proxy profiles",
    "clients": "Torrent clients",
    "notifications": "Notifications",
    "events": "Events",
    "settings": "Settings",
    "system": "System status",
}


@router.get("/{page}", response_class=HTMLResponse, include_in_schema=False)
def resource_page(page: str, request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    if page not in _PAGES:
        raise HTTPException(status_code=404, detail="Page not found.")
    service = admin_service(request)
    if page == "trackers":
        rows: Any = service.trackers()
    elif page == "settings":
        rows = service.settings()
    elif page == "system":
        rows = service.system()
    elif page == "events":
        rows = service.events()
    elif page == "tracker-accounts":
        rows = []
    else:
        rows = service.resource(page)
    return templates.TemplateResponse(
        request=request,
        name="resource.html",
        context=page_context(request, user, title=_PAGES[page], rows=rows),
    )
