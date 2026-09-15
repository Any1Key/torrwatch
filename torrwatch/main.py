"""FastAPI web process entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from torrwatch.api.routes import router, templates
from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.security import USER_SESSION_KEY, csrf_token
from torrwatch.db.bootstrap import bootstrap_admin
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import SystemSetting
from torrwatch.trackers.loader import load_plugin_registry
from torrwatch.web.admin import router as browser_router

logger = logging.getLogger(__name__)


public_router = APIRouter()


@public_router.get("/", response_class=HTMLResponse, include_in_schema=False)
def public_home(request: Request):
    if isinstance(request.session.get(USER_SESSION_KEY), int):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token(request), "error": None},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the independent web application process."""
    active_settings = settings or get_settings()
    database = Database(active_settings.resolved_database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        try:
            ensure_runtime_files(active_settings)
            upgrade_database(active_settings)
            with database.session() as session:
                debug = (
                    session.scalar(
                        select(SystemSetting.value).where(SystemSetting.key == "debug_mode")
                    )
                    == "true"
                )
            logging.getLogger().setLevel(
                logging.DEBUG
                if debug
                else getattr(logging, active_settings.log_level.upper(), logging.INFO)
            )
            bootstrap_admin(database, active_settings)
            app.state.plugin_registry = load_plugin_registry(active_settings)
            app.state.ready = True
        except Exception:
            logger.exception("Web bootstrap failed; readiness is unavailable")
        yield
        database.dispose()

    app = FastAPI(title="TorrWatch", version="0.1.0", lifespan=lifespan)
    app.state.database = database
    app.state.settings = active_settings
    app.state.ready = False
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(active_settings),
        max_age=active_settings.session_max_age_seconds,
        same_site="lax",
        https_only=active_settings.session_https_only,
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )
    app.include_router(public_router)
    app.include_router(browser_router)
    app.include_router(router)
    return app


def _session_secret(settings: Settings) -> str:
    """Use a private, persistent value without putting it in SQLite or logs."""
    ensure_runtime_files(settings)
    return settings.resolved_master_key_file.read_bytes().hex()


def run() -> None:
    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port)
