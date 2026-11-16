"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from . import __version__
from .auth import CSRF_HEADER, AuthService
from .bots import BotService
from .config import Settings
from .crypto import Crypto
from .db import Database
from .events import EventHub
from .github import GitHubService
from .paths import UnsafePath, clean_relpath, resolve_inside
from .security import (JSON_BODY_CAP, PUBLIC_BODY_CAP, PUBLIC_BODY_PATHS, BodyTooLarge, host_allowed,
                       new_setup_code, origin_matches, read_password_file, split_host, weak_password_reason,
                       write_secret_file)
from .supervisor import Supervisor
from .util import ApiError

log = logging.getLogger("botdock")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_JSON_BODY = 16 * 1024 * 1024
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'self'; img-src 'self' data: https://avatars.githubusercontent.com; style-src 'self' 'unsafe-inline'"),
]
MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".map": "application/json",
    ".txt": "text/plain; charset=utf-8", ".webmanifest": "application/manifest+json",
}


class AppContext:
    def __init__(self, settings: Settings, github_transport=None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.bots_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.db_path)
        self.crypto = Crypto(settings.key_path, settings.secret_key)
        self.auth = AuthService(self.db, settings.session_days, settings.login_max_failures,
                                settings.login_window_seconds)
        self.events = EventHub()
        self.supervisor = Supervisor(settings, self.db, self.crypto, on_change=self._on_change)
        self.bots = BotService(settings, self.db, self.crypto, self.supervisor, self.events)
        self.github = GitHubService(settings, self.db, self.crypto, self.supervisor, self.bots,
                                    transport=github_transport)
        self.bots.github = self.github
        self.started_at = time.time()
        self.streams: dict = {}
        self.setup_code: str | None = None
        self.shutdown_event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # SSE streams by session token, so ending a session can cut its streams immediately.
    def register_stream(self, sub, token: str | None) -> None:
        self.streams[sub] = token

    def unregister_stream(self, sub) -> None:
        self.streams.pop(sub, None)

    def close_streams(self, *, token: str | None = None, except_token: str | None = None) -> int:
        """Revoke the streams of one session (token=...) or of every other session (except_token=...)."""
        n = 0
        for sub, tok in list(self.streams.items()):
            if (token is not None and tok == token) or (except_token is not None and tok != except_token):
                sub.revoked = True
                sub.close()
                n += 1
        return n

    # first-run admin setup ------------------------------------------------------------
    @property
    def setup_code_path(self) -> Path:
        return self.settings.data_dir / "setup-code.txt"

    def prepare_admin(self) -> None:
        """Seed the admin password from BOTDOCK_PASSWORD_FILE / BOTDOCK_PASSWORD (unless weak), otherwise
        create a one-time setup code that first-run setup must present."""
        s = self.settings
        seed, origin = None, None
        if s.password_file:
            seed, origin = read_password_file(s.password_file), "BOTDOCK_PASSWORD_FILE"
        if seed is None and s.password:
            seed, origin = s.password, "BOTDOCK_PASSWORD"
        if seed and not self.auth.is_setup():
            reason = weak_password_reason(seed)
            if reason:
                log.warning("NOT using the password from %s: it is %s. Falling back to first-run setup with a "
                            "setup code.", origin, reason)
            else:
                self.auth.set_password(seed)
                log.info("Admin password seeded from %s", origin)
        if self.auth.is_setup():
            self.setup_code = None
            try:
                self.setup_code_path.unlink()
            except OSError:
                pass
            return
        self.setup_code = new_setup_code()
        write_secret_file(self.setup_code_path, self.setup_code + "\n")
        rule = "=" * 64
        print(f"\n{rule}\n  BotDock first-run setup code:  {self.setup_code}\n"
              f"  (also in {self.setup_code_path}; enter it with your new password)\n{rule}\n", flush=True)

    def finish_setup(self) -> None:
        self.setup_code = None
        try:
            self.setup_code_path.unlink()
        except OSError:
            pass

    def _on_change(self, bot_id: str) -> None:
        self.bots.publish(bot_id)

    @property
    def shutting_down(self) -> bool:
        return self.shutdown_event is not None and self.shutdown_event.is_set()

    def begin_shutdown(self) -> None:
        """End all SSE streams so the HTTP server can finish draining connections."""
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        self.events.close_all()
        for rt in self.supervisor.runtimes.values():
            for sub in list(rt.subscribers):
                sub.close()

    def begin_shutdown_threadsafe(self) -> None:
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.begin_shutdown)
            except RuntimeError:
                pass


class SecurityMiddleware:
    """Pure ASGI middleware:
    - Host allow-list on every request (DNS rebinding), Origin == Host on mutating /api requests;
    - CSRF header on mutating /api requests;
    - non-multipart /api request bodies capped on the bytes actually received (64 KB for the
      unauthenticated login/setup endpoints, 16 MB otherwise), whatever Content-Length says;
    - security headers on every response."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS:
                    if k not in present:
                        headers.append((k, v))
                if message.get("status", 200) >= 400 and scope["method"] not in SAFE_METHODS \
                        and b"connection" not in present:
                    # The request body may be unread (401 before an upload, 413...): close the connection
                    # instead of letting the server read and discard the rest of it.
                    headers.append((b"connection", b"close"))
                message = {**message, "headers": headers}
            await send(message)

        def reject(status: int, detail: str):
            return JSONResponse({"detail": detail}, status)(scope, receive, send_wrapper)

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1")
        if not host_allowed(host, self.settings.allowed_hosts):
            shown = split_host(host)[0][:100] or "(none)"
            req_path = scope.get("path", "")
            if not (req_path == "/api" or req_path.startswith("/api/")):
                # A person opening the UI under an unexpected name gets a readable page, not raw JSON.
                import html as _html
                page = ("<!doctype html><meta charset=utf-8><title>BotDock</title>"
                        "<body style='font:16px system-ui;max-width:36em;margin:15vh auto;padding:0 1em'>"
                        "<h1>BotDock refused this address</h1>"
                        f"<p>The name <b>{_html.escape(shown)}</b> is not on BotDock's list of allowed host names. "
                        "Open BotDock by its IP address, or add this name to the "
                        "<code>BOTDOCK_ALLOWED_HOSTS</code> setting (comma separated) and restart the app.</p>")
                from starlette.responses import HTMLResponse
                return await HTMLResponse(page, 403)(scope, receive, send_wrapper)
            return await reject(403, f"Host {shown!r} is not allowed. If you reach BotDock through this name, add it "
                                     f"to BOTDOCK_ALLOWED_HOSTS (comma separated; '*' disables the check).")
        path = scope.get("path", "")
        if path == "/api" or path.startswith("/api/"):
            mutating = scope["method"] not in SAFE_METHODS
            if mutating and headers.get(CSRF_HEADER.encode()) != b"1":
                return await reject(403, "Missing or invalid X-BotDock header")
            origin = headers.get(b"origin")
            if mutating and origin is not None and not origin_matches(
                    origin.decode("latin-1"), host, scope.get("scheme", "http")):
                return await reject(403, "Cross-origin request refused (Origin does not match Host)")
            ctype = headers.get(b"content-type", b"")
            if not ctype.startswith(b"multipart/"):
                cap = PUBLIC_BODY_CAP if path in PUBLIC_BODY_PATHS else JSON_BODY_CAP
                length = headers.get(b"content-length", b"")
                if length.isdigit() and int(length) > cap:
                    return await reject(413, "Request body too large")
                received = 0
                inner_receive = receive

                async def capped_receive():
                    nonlocal received
                    message = await inner_receive()
                    if message["type"] == "http.request":
                        received += len(message.get("body", b""))
                        if received > cap:
                            raise BodyTooLarge()
                    return message

                receive = capped_receive
        await self.app(scope, receive, send_wrapper)


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Invalid request"
    err = errors[0]
    loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
    msg = err.get("msg", "invalid value")
    if err.get("type") == "json_invalid":
        return "Request body is not valid JSON"
    return f"{'.'.join(loc)}: {msg}" if loc else msg


def create_app(data_dir: str | Path | None = None, settings: Settings | None = None,
               github_transport=None) -> FastAPI:
    """github_transport: optional httpx transport for all GitHub traffic (tests use a fake)."""
    if settings is None:
        settings = Settings.from_env(data_dir=Path(data_dir) if data_dir else None)
    ctx = AppContext(settings, github_transport=github_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.loop = asyncio.get_running_loop()
        ctx.shutdown_event = asyncio.Event()
        ctx.prepare_admin()  # seed password / setup code, before any bot starts
        shutil.rmtree(settings.tmp_dir, ignore_errors=True)
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        log.info("BotDock %s, data dir %s", __version__, settings.data_dir)
        await ctx.supervisor.startup()
        await ctx.github.startup()

        async def stats_loop() -> None:
            while True:
                await asyncio.sleep(settings.stats_interval)
                try:
                    ctx.supervisor.refresh_stats()
                    for bot_id, rt in list(ctx.supervisor.runtimes.items()):
                        if rt.state == "running":
                            ctx.bots.publish(bot_id)
                except Exception:  # pragma: no cover
                    log.exception("stats refresh failed")

        stats_task = asyncio.create_task(stats_loop())
        try:
            yield
        finally:
            ctx.begin_shutdown()
            stats_task.cancel()
            await ctx.github.shutdown()
            await ctx.supervisor.shutdown()
            ctx.db.close()
            shutil.rmtree(settings.tmp_dir, ignore_errors=True)

    app = FastAPI(title="BotDock", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": _validation_message(exc)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    from .api import github as github_api
    from .api import auth as auth_api, bots as bots_api, env as env_api, files as files_api, logs as logs_api, system as system_api

    for module in (auth_api, system_api, github_api, bots_api, env_api, logs_api, files_api):
        app.include_router(module.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def api_not_found(rest: str):
        raise ApiError(404, "Not found")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        return serve_frontend(full_path)

    app.add_middleware(SecurityMiddleware, settings=settings)
    return app


def serve_frontend(full_path: str) -> Response:
    if full_path:
        try:
            rel = clean_relpath(full_path)
            candidate = resolve_inside(STATIC_DIR, rel)
            if candidate.is_file():
                media = MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
                return FileResponse(candidate, media_type=media, headers={"Cache-Control": "no-cache"})
        except (UnsafePath, OSError):
            pass
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("BotDock frontend not installed (botdock/static/index.html is missing).\n"
                             "The API is available under /api/.", status_code=503)
