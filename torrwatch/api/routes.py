"""Phase 0 browser and health routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from torrwatch.core.security import (
    USER_SESSION_KEY,
    csrf_token,
    require_admin,
    require_csrf,
    verify_password,
)
from torrwatch.db.models import User

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "web" / "templates")
)


@router.get("/health/live", include_in_schema=False)
def live() -> JSONResponse:
    return JSONResponse({"status": "live"})


@router.get("/health/ready", include_in_schema=False)
def ready(request: Request) -> JSONResponse:
    if not request.app.state.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Application is not ready."
        )
    try:
        with request.app.state.database.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database is unavailable."
        ) from error
    return JSONResponse({"status": "ready"})


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(
        "# HELP torrwatch_phase0_info TorrWatch bootstrap information\n"
        "# TYPE torrwatch_phase0_info gauge\n"
        "torrwatch_phase0_info 1\n",
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
    password: str = Form(),
    csrf: str = Form(),
) -> Response:
    require_csrf(request, csrf)
    with request.app.state.database.session() as database_session:
        user = database_session.query(User).filter(User.username == username).one_or_none()
        if user is None or user.disabled or not verify_password(user.password_hash, password):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf_token": csrf_token(request), "error": "Неверное имя или пароль."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        request.session[USER_SESSION_KEY] = user.id
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", include_in_schema=False)
def logout(
    request: Request, csrf: str = Form(), _: User = Depends(require_admin)
) -> RedirectResponse:
    require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"csrf_token": csrf_token(request), "username": user.username},
    )
