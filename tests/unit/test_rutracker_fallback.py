import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx

from torrwatch.core.config import Settings
from torrwatch.services.flaresolverr import SolverResult
from torrwatch.services.rutracker_fallback import solve_rutracker_topic
from torrwatch.transport.http import TransportResponse


def test_challenge_uses_browser_page_and_preserves_session(monkeypatch):
    url = "https://rutracker.org/forum/viewtopic.php?t=123"
    html = '<meta property="og:title" content="Название"><p>Вход и login</p><a href="dl.php?t=123">download</a>'
    state = {}

    class DB:
        @contextmanager
        def session(self):
            yield SimpleNamespace(execute=lambda query: SimpleNamespace(all=lambda: []))

    class Sessions:
        def load(self, namespace):
            return httpx.Cookies(), None

        def save(self, namespace, cookies, user_agent):
            state["saved"] = (namespace, cookies.get("bb_session"), user_agent)

    class Solver:
        def __init__(self, endpoint):
            pass

        async def request(self, **kwargs):
            state["session"] = kwargs["session"]
            return SolverResult(
                [{"name": "bb_session", "value": "test", "domain": ".rutracker.org"}],
                "browser",
                html,
                200,
            )

    monkeypatch.setattr("torrwatch.services.rutracker_fallback.FlareSolverrClient", Solver)
    initial = TransportResponse(403, {"cf-mitigated": "challenge"}, b"", url, 1, 1)
    result = asyncio.run(
        solve_rutracker_topic(
            url,
            initial,
            database=DB(),
            settings=Settings(flaresolverr_url="http://solver"),
            sessions=Sessions(),
            namespace="rutracker:account:1",
        )
    )
    assert result.status_code == 200 and "Название" in result.text
    assert state["saved"] == ("rutracker:account:1", "test", "browser")
    assert state["session"] == "torrwatch-rutracker-account-1"


def test_challenge_html_with_http_200_uses_browser_page(monkeypatch):
    url = "https://rutracker.org/forum/viewtopic.php?t=123"
    html = '<meta property="og:title" content="Название"><a href="dl.php?t=123">download</a>'

    class DB:
        @contextmanager
        def session(self):
            yield SimpleNamespace(execute=lambda query: SimpleNamespace(all=lambda: []))

    class Sessions:
        def load(self, namespace):
            return httpx.Cookies(), None

        def save(self, namespace, cookies, user_agent):
            pass

    class Solver:
        def __init__(self, endpoint):
            pass

        async def create_session(self, session):
            pass

        async def request(self, **kwargs):
            return SolverResult(
                [{"name": "bb_session", "value": "test", "domain": ".rutracker.org"}],
                "browser",
                html,
                200,
            )

        async def destroy_session(self, session):
            pass

    monkeypatch.setattr("torrwatch.services.rutracker_fallback.FlareSolverrClient", Solver)
    initial = TransportResponse(200, {}, b"<title>Just a moment...</title>", url, 1, 1)
    result = asyncio.run(
        solve_rutracker_topic(
            url,
            initial,
            database=DB(),
            settings=Settings(flaresolverr_url="http://solver"),
            sessions=Sessions(),
            namespace="rutracker:account:1",
        )
    )
    assert result.status_code == 200 and "Название" in result.text
