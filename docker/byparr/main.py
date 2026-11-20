"""TorrWatch browser solver with form POST and cookie handoff support."""

from __future__ import annotations

import asyncio
import logging
import time
from asyncio import sleep
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from invisible_playwright.async_api import Error as PlaywrightError
from invisible_playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import Field
from src.challenge import (
    challenge_present,
    checkbox_already_answered,
    click_checkbox,
    solve_challenge,
)
from src.consts import HOST, LOG_LEVEL, PORT
from src.content import build_response_content
from src.endpoints import BrowserDep, _navigate_and_solve, _wait_for_networkidle, setup_routes
from src.middlewares import LogRequest
from src.models import LinkRequest, LinkResponse, Solution
from src.utils import TimeoutTimer, remaining_ms, warm_up


class TorrWatchRequest(LinkRequest):
    post_data: str | None = Field(default=None, alias="postData")
    cookies: list[dict[str, object]] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await warm_up()
    yield


app = FastAPI(debug=LOG_LEVEL == logging.DEBUG, lifespan=lifespan)
app.add_middleware(GZipMiddleware)
app.add_middleware(LogRequest)
browser_lock = asyncio.Lock()


async def _solve_embedded_turnstile(page, timer: TimeoutTimer) -> None:
    """Answer a Turnstile widget embedded in a normal login form."""
    token = page.locator('input[name="cf-turnstile-response"]')
    if await token.count() == 0:
        return
    while timer.remaining() > 0:
        if await checkbox_already_answered(page):
            return
        with suppress(PlaywrightError, PlaywrightTimeoutError):
            await click_checkbox(page)
        await sleep(0.5)
    raise TimeoutError("Embedded Turnstile was not completed")


async def _submit_form(request: TorrWatchRequest, dep, timer: TimeoutTimer) -> None:
    fields = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
    password = dep.page.locator(
        'input[type="password"], input[name="password"], input[name="login_password"]'
    )
    if await password.count() == 0:
        raise HTTPException(
            status_code=422,
            detail=f"Login form was not found at {dep.page.url}",
        )
    form = password.first.locator("xpath=ancestor::form[1]")
    for name, value in fields.items():
        control = form.locator(f'[name="{name}"]')
        if await control.count() == 0 or name == "cf-turnstile-response":
            continue
        kind = (await control.first.get_attribute("type") or "").lower()
        if kind in {"submit", "button", "image", "hidden"}:
            continue
        if kind in {"checkbox", "radio"}:
            if value:
                await control.first.check(timeout=remaining_ms(timer))
        else:
            await control.first.fill(value, timeout=remaining_ms(timer))
    await _solve_embedded_turnstile(dep.page, timer)
    submit = form.locator('input[type="submit"], button[type="submit"], button')
    if await submit.count():
        await submit.first.click(timeout=remaining_ms(timer))
    else:
        await form.evaluate("form => form.requestSubmit()")
    await dep.page.wait_for_load_state("domcontentloaded", timeout=remaining_ms(timer))
    if await challenge_present(dep.page):
        await solve_challenge(dep.page, timer)
    await _wait_for_networkidle(dep, timer)


@app.get("/health")
async def health():
    return {"msg": "TorrWatch browser solver is working"}


@app.post("/v1")
async def solve(request: TorrWatchRequest, dep: BrowserDep):
    async with browser_lock:
        return await _solve_locked(request, dep)


async def _solve_locked(request: TorrWatchRequest, dep):
    started = int(time.time() * 1000)
    timer = TimeoutTimer(duration=request.max_timeout)
    request.url = request.url.replace('"', "").strip()
    try:
        if request.cookies:
            allowed = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}
            cookies = [
                {key: value for key, value in item.items() if key in allowed and value is not None}
                for item in request.cookies
            ]
            await dep.context.add_cookies(cookies)
        await setup_routes(request, dep)
        page_request = await _navigate_and_solve(dep, request, timer)
        if request.cmd == "request.post" and request.post_data is not None:
            await _submit_form(request, dep, timer)
    except (TimeoutError, PlaywrightTimeoutError) as error:
        raise HTTPException(status_code=408, detail="Browser operation timed out") from error
    except PlaywrightError as error:
        raise HTTPException(status_code=502, detail=f"Browser operation failed: {error}") from error

    cookie_urls = [request.url]
    if dep.page.url and dep.page.url not in cookie_urls:
        cookie_urls.append(dep.page.url)
    cookies = await dep.context.cookies(cookie_urls)
    content_type, content = await build_response_content(dep.page, request, page_request)
    user_agent = await dep.page.evaluate("navigator.userAgent")
    return LinkResponse(
        message="Success",
        solution=Solution(
            user_agent=user_agent,
            url=dep.page.url,
            status=HTTPStatus.OK,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=content,
            content_type=content_type,
        ),
        start_timestamp=started,
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level=logging.getLevelName(LOG_LEVEL).lower())
