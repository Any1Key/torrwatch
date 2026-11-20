from unittest.mock import AsyncMock

import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import Base, TrackerSession
from torrwatch.services.flaresolverr import (
    FlareSolverrError,
    SolverResult,
    refresh_kinozal,
    refresh_nnmclub,
    refresh_rutracker,
)
from torrwatch.transport.cookies import SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refresh,tracker",
    [(refresh_nnmclub, "nnmclub"), (refresh_kinozal, "kinozal"), (refresh_rutracker, "rutracker")],
)
@pytest.mark.parametrize("outcome", ["success", "login", "challenge", "http_error"])
async def test_session_refresh_preserves_good_cookies_until_authenticated(
    tmp_path, settings, refresh, tracker, outcome
):
    db = Database(f"sqlite:///{tmp_path}/db")
    Base.metadata.create_all(db.engine)
    ensure_runtime_files(settings)
    box = SecretBox(settings.resolved_master_key_file)
    store = SessionStore(db, box)
    namespace = f"{tracker}:account:1"
    store.import_cookie_header(namespace, "sid=old")
    solver = AsyncMock()
    html = {
        "success": "<html>Welcome</html>",
        "login": '<input name="password">',
        "challenge": "<title>Just a moment</title>",
        "http_error": "Unavailable",
    }[outcome]
    account_cookies = {
        "nnmclub": ["phpbb2mysql_4_data", "phpbb2mysql_4_sid"],
        "kinozal": ["uid", "pass"],
        "rutracker": ["bb_session"],
    }[tracker]
    solver.request.return_value = SolverResult(
        [{"name": "sid", "value": "new", "domain": f".{tracker}.to"}]
        + [
            {"name": name, "value": "account", "domain": f".{tracker}.to"}
            for name in account_cookies
        ],
        "Browser",
        html,
        503 if outcome == "http_error" else 200,
    )
    if outcome == "success":
        await refresh(db, box, solver, "user", "password")
        assert store.load(namespace)[0].get("sid") == "new"
        with db.session() as session:
            assert session.query(TrackerSession).one().auth_status == "WORKING"
    else:
        with pytest.raises(FlareSolverrError):
            await refresh(db, box, solver, "user", "password")
        assert store.load(namespace)[0].get("sid") == "old"
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refresh,tracker",
    [(refresh_nnmclub, "nnmclub"), (refresh_kinozal, "kinozal"), (refresh_rutracker, "rutracker")],
)
async def test_login_is_submitted_as_one_browser_operation(
    tmp_path, settings, refresh, tracker
):
    ensure_runtime_files(settings)
    db = Database(f"sqlite:///{tmp_path}/login.db")
    Base.metadata.create_all(db.engine)
    box = SecretBox(settings.resolved_master_key_file)
    solver = AsyncMock()
    account_cookies = {
        "nnmclub": ["phpbb2mysql_4_data", "phpbb2mysql_4_sid"],
        "kinozal": ["uid", "pass"],
        "rutracker": ["bb_session"],
    }[tracker]
    success = SolverResult(
        [{"name": "sid", "value": "good"}]
        + [{"name": name, "value": "account"} for name in account_cookies],
        "browser",
        "<html>Welcome</html>",
        200,
    )
    solver.request.return_value = success
    await refresh(db, box, solver, "user", "password")
    posts = [
        call.kwargs["post_data"]
        for call in solver.request.call_args_list
        if "post_data" in call.kwargs
    ]
    assert len(posts) == 1 and "password" in posts[0]
    assert SessionStore(db, box).load(f"{tracker}:account:1")[0].get("sid") == "good"
    db.dispose()
