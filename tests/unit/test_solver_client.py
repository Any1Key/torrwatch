import httpx
import pytest

from torrwatch.services.flaresolverr import FlareSolverrClient, FlareSolverrError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "http", "invalid_json", "error", "invalid_solution"])
async def test_solver_response_validation(monkeypatch, mode):
    real_client = httpx.AsyncClient
    seen = []

    def handle(request):
        seen.append(request)
        if mode == "http":
            return httpx.Response(500)
        if mode == "invalid_json":
            return httpx.Response(200, text="not json")
        if mode == "error":
            return httpx.Response(200, json={"status": "error", "message": "Challenge failed"})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {}
                if mode == "invalid_solution"
                else {"cookies": [], "userAgent": "browser", "response": "welcome", "status": 200},
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    client = FlareSolverrClient("http://solver")
    if mode == "ok":
        result = await client.request(url="https://nnmclub.to", session="test", post_data="a=b")
        assert result.status == 200 and result.html == "welcome"
    else:
        with pytest.raises(FlareSolverrError):
            await client.request(url="https://nnmclub.to", session="test")
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_reset_recreates_expired_browser(monkeypatch, missing):
    import json

    real_client = httpx.AsyncClient
    commands = []

    def handle(request):
        command = json.loads(request.content)["cmd"]
        commands.append(command)
        return httpx.Response(
            500 if missing and command == "sessions.destroy" else 200, json={"status": "ok"}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    await FlareSolverrClient("http://solver").reset_session("test")
    assert commands == ["sessions.destroy", "sessions.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body", [(500, {}), (200, {"status": "error", "message": "failed"})]
)
async def test_session_creation_failure_is_reported(monkeypatch, status, body):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)), **kw
        ),
    )
    with pytest.raises(FlareSolverrError):
        await FlareSolverrClient("http://solver").create_session("test")


@pytest.mark.asyncio
async def test_byparr_falls_back_to_cookie_handoff(monkeypatch):
    import json

    real_client = httpx.AsyncClient
    commands = []

    def handle(request):
        command = json.loads(request.content)["cmd"]
        commands.append(command)
        return httpx.Response(
            502,
            json={
                "detail": 'Could not reach the target: Page.navigate: Invalid url: "https://"'
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)
    )
    client = FlareSolverrClient("http://solver")
    await client.reset_session("test")
    await client.reset_session("test")
    assert commands == ["sessions.destroy"]
