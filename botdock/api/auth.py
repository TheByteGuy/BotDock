from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..security import normalize_code
from ..auth import COOKIE_NAME, MAX_PASSWORD_LEN, MIN_PASSWORD_LEN
from ..util import ApiError
from .deps import Authed, get_ctx, json_body

router = APIRouter(prefix="/api")


def _set_cookie(request: Request, response: JSONResponse, token: str) -> None:
    ctx = get_ctx(request)
    response.set_cookie(
        COOKIE_NAME, token, max_age=ctx.settings.session_days * 86400, path="/", httponly=True,
        samesite="strict", secure=ctx.settings.cookie_secure,
    )


def _password(data: dict, key: str = "password") -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise ApiError(400, f"{key} is required")
    if len(value) > MAX_PASSWORD_LEN:
        raise ApiError(400, f"{key} is too long")
    return value


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/health")
async def health():
    return {"status": "ok", "version": __version__}


@router.get("/auth/state")
async def auth_state(request: Request):
    ctx = get_ctx(request)
    return {
        "setup_required": not ctx.auth.is_setup(),
        "authenticated": ctx.auth.validate_session(request.cookies.get(COOKIE_NAME)),
        "setup_code_required": not ctx.auth.is_setup() and ctx.setup_code is not None,
    }


@router.post("/auth/setup")
async def auth_setup(request: Request):
    ctx = get_ctx(request)
    ip = _client_ip(request)
    if ctx.auth.limiter.is_limited(ip):
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    data = await json_body(request)
    password = _password(data)
    if ctx.auth.is_setup():
        raise ApiError(409, "Already set up")
    if ctx.setup_code is not None:
        given = normalize_code(data.get("setup_code"))
        if not given or not hmac.compare_digest(given, normalize_code(ctx.setup_code)):
            ctx.auth.limiter.record_failure(ip)
            raise ApiError(403, "Wrong or missing setup code. It is printed in the BotDock log and stored in "
                                "setup-code.txt in the data folder.")
    if len(password) < MIN_PASSWORD_LEN:
        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")
    ctx.auth.set_password(password)
    ctx.auth.limiter.reset(ip)
    ctx.finish_setup()
    response = JSONResponse({"ok": True})
    _set_cookie(request, response, ctx.auth.create_session())
    return response


@router.post("/auth/login")
async def auth_login(request: Request):
    ctx = get_ctx(request)
    ip = _client_ip(request)
    if ctx.auth.limiter.is_limited(ip):
        raise ApiError(429, "Too many failed login attempts. Try again in a few minutes.")
    data = await json_body(request)
    password = _password(data)
    if not ctx.auth.is_setup():
        raise ApiError(409, "Setup required")
    if not ctx.auth.check_password(password):
        ctx.auth.limiter.record_failure(ip)
        raise ApiError(401, "Wrong password")
    ctx.auth.limiter.reset(ip)
    response = JSONResponse({"ok": True})
    _set_cookie(request, response, ctx.auth.create_session())
    return response


@router.post("/auth/logout", dependencies=Authed)
async def auth_logout(request: Request):
    ctx = get_ctx(request)
    ctx.auth.delete_session(request.cookies.get(COOKIE_NAME))
    ctx.close_streams(token=request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict",
                           secure=ctx.settings.cookie_secure)
    return response


@router.post("/auth/password", dependencies=Authed)
async def auth_password(request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    current = _password(data, "current_password")
    new = _password(data, "new_password")
    if not ctx.auth.check_password(current):
        raise ApiError(400, "Current password is incorrect")
    if len(new) < MIN_PASSWORD_LEN:
        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")
    ctx.auth.set_password(new)
    ctx.auth.delete_other_sessions(request.cookies.get(COOKIE_NAME))
    ctx.close_streams(except_token=request.cookies.get(COOKIE_NAME))
    return {"ok": True}
