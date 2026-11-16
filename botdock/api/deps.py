"""Shared dependencies and helpers for routers."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import Depends, Request

from ..app import AppContext
from ..auth import COOKIE_NAME
from ..util import ApiError


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_session(request: Request) -> None:
    ctx: AppContext = request.app.state.ctx
    if not ctx.auth.validate_session(request.cookies.get(COOKIE_NAME)):
        raise ApiError(401, "Not authenticated")


Authed = [Depends(require_session)]


async def json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "Request body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return data


def require_str(data: dict, key: str, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and value == ""):
        raise ApiError(400, f"{key} is required")
    return value


def content_disposition(filename: str) -> str:
    ascii_name = "".join(c if (c.isascii() and (c.isalnum() or c in "._- ")) else "_" for c in filename).strip()
    return f"attachment; filename=\"{ascii_name or 'download'}\"; filename*=UTF-8''{quote(filename, safe='')}"


def sse(event: str | None, data, event_id: int | None = None) -> str:
    out = ""
    if event_id is not None:
        out += f"id: {event_id}\n"
    if event:
        out += f"event: {event}\n"
    return out + f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


SESSION_RECHECK_SECONDS = 2.0


async def sse_pump(request: Request, open_stream, render):
    """Drive one SSE stream for the requesting session.

    open_stream() -> (subscriber, prelude chunks, close function). It is called when the response
    actually starts streaming, so a client that disconnects earlier never leaves a subscriber behind,
    and the close function always runs once it was opened.

    The stream is registered against its session token so logout / password change close it at
    once (AppContext.close_streams), and the session is re-validated every couple of seconds as a
    backstop (expiry, revocation from elsewhere). A revoked stream ends without delivering anything
    that was still queued. render(item) -> str | None.
    """
    import time

    ctx: AppContext = request.app.state.ctx
    token = request.cookies.get(COOKIE_NAME)
    heartbeat = ctx.settings.heartbeat_interval
    sub, prelude, close = open_stream()
    ctx.register_stream(sub, token)
    try:
        yield "retry: 3000\n: connected\n\n"
        for chunk in prelude:
            yield chunk
        last_beat = last_check = time.monotonic()
        while not ctx.shutting_down:
            got = await sub.wait(min(SESSION_RECHECK_SECONDS, heartbeat))
            if ctx.shutting_down or sub.revoked:
                break
            now = time.monotonic()
            if now - last_check >= SESSION_RECHECK_SECONDS:
                last_check = now
                if not ctx.auth.validate_session(token):
                    break
            items = sub.drain() if got else []
            for item in items:
                out = render(item)
                if out:
                    yield out
                    last_beat = now
            if now - last_beat >= heartbeat:
                yield ": heartbeat\n\n"
                last_beat = now
            if sub.closed:
                break
    finally:
        ctx.unregister_stream(sub)
        close()
