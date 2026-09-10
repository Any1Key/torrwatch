"""FastAPI web process entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from torrwatch.api.routes import router
from torrwatch.core.config import Settings, get_settings
from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.bootstrap import bootstrap_admin
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database

logger = logging.getLogger(__name__)


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
            bootstrap_admin(database, active_settings)
            app.state.ready = True
        except Exception:
            logger.exception("Web bootstrap failed; readiness is unavailable")
        yield
        database.dispose()

    app = FastAPI(title="TorrWatch", version="0.1.0", lifespan=lifespan)
    app.state.database = database
    app.state.ready = False
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(active_settings),
        max_age=active_settings.session_max_age_seconds,
        same_site="lax",
        https_only=active_settings.session_https_only,
    )
    app.include_router(router)
    return app


def _session_secret(settings: Settings) -> str:
    """Use a private, persistent value without putting it in SQLite or logs."""
    ensure_runtime_files(settings)
    return settings.resolved_master_key_file.read_bytes().hex()


def run() -> None:
    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port)
