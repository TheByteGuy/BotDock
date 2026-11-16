"""GitHub integration: sign-in (device flow / PAT), repo listing, repo-backed bots, sync, poller.

All GitHub HTTP goes through ``GitHubClient`` (async httpx, so nothing blocks the event loop). Its
base URLs come from settings (``BOTDOCK_GITHUB_API_URL`` / ``BOTDOCK_GITHUB_URL``), which is how tests
point it at a fake GitHub and how GitHub Enterprise would work.

Token hygiene: the token lives only Fernet-encrypted in the DB and in memory while a request is made.
It is sent only to the API host (never forwarded on the tarball redirect to codeload), never put in
error messages (all user-visible errors are fixed strings) and never in a bot's environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .config import Settings
from .crypto import Crypto, InvalidToken
from .db import Database
from .paths import UnsafePath, clean_relpath, resolve_inside
from .uploads import is_skipped
from .util import ApiError, iso

log = logging.getLogger("botdock.github")
# httpx logs every request URL at INFO; codeload redirect URLs carry short-lived credentials.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

NOT_CONNECTED = "GitHub is not connected"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
REPOS_PAGE_SIZE = 30
REPO_CACHE_SECONDS = 60.0
MAX_REPO_PAGES = 10
DEFAULT_POLL_INTERVAL = 120

KV_TOKEN, KV_METHOD, KV_LOGIN, KV_NAME, KV_AVATAR = (
    "github_token", "github_method", "github_login", "github_name", "github_avatar")
KV_CLIENT_ID, KV_POLL = "github_client_id", "github_poll_interval"


# Errors ----------------------------------------------------------------------------------------


class GitHubError(Exception):
    """Carries a fixed, user-safe message (never a token, URL with credentials or raw body)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TokenRejected(GitHubError):
    pass


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, until: float):
        super().__init__("Rate limited by GitHub until " + (iso(until) or "later"))
        self.until = until


def validate_repo(repo: Any) -> str:
    if not isinstance(repo, str) or not REPO_RE.match(repo.strip()) or ".." in repo:
        raise ApiError(400, "repo must look like owner/name")
    return repo.strip()


def validate_branch(branch: Any) -> str:
    if not isinstance(branch, str) or not branch.strip():
        raise ApiError(400, "branch is required")
    b = branch.strip()
    if len(b) > 255 or b.startswith("-") or ".." in b or any(ord(c) < 33 or c in "~^:?*[\\" for c in b):
        raise ApiError(400, "Invalid branch name")
    return b


def validate_subdir(subdir: Any) -> str:
    if subdir is None:
        return ""
    if not isinstance(subdir, str):
        raise ApiError(400, "subdir must be a string")
    return clean_relpath(subdir, allow_root=True)


# HTTP client -----------------------------------------------------------------------------------


class GitHubClient:
    def __init__(self, api_url: str, web_url: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30.0):
        self.api_url = api_url.rstrip("/")
        self.web_url = web_url.rstrip("/")
        self._api_netloc = urlsplit(self.api_url).netloc.lower()
        self._transport = transport
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self.rate_remaining: int | None = None

    @property
    def _client(self) -> httpx.AsyncClient:
        # created lazily: building the TLS context costs ~0.2 s and most runs never talk to GitHub
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, follow_redirects=False, trust_env=False,
                headers={"User-Agent": "BotDock", "X-GitHub-Api-Version": "2022-11-28"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _auth(self, url: str, token: str | None) -> dict[str, str]:
        # Only ever send the token to the API host itself.
        if token and urlsplit(url).netloc.lower() == self._api_netloc:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _track(self, r: httpx.Response) -> None:
        rem = r.headers.get("x-ratelimit-remaining")
        if rem is not None and rem.isdigit():
            self.rate_remaining = int(rem)

    @staticmethod
    def _raise(r: httpx.Response, not_found: str) -> None:
        code = r.status_code
        if code == 401:
            raise TokenRejected("GitHub rejected the token (sign in again)")
        if code in (403, 429):
            retry_after = r.headers.get("retry-after", "")
            remaining = r.headers.get("x-ratelimit-remaining")
            if retry_after.isdigit():
                raise RateLimited(time.time() + int(retry_after))
            if remaining == "0" or code == 429:
                reset = r.headers.get("x-ratelimit-reset", "")
                raise RateLimited(float(reset) if reset.isdigit() else time.time() + 60)
            raise GitHubError("GitHub denied access (HTTP 403)")
        if code == 404:
            raise NotFound(not_found)
        raise GitHubError(f"GitHub returned HTTP {code}")

    async def api(self, path: str, token: str | None, *, params: dict | None = None, etag: str | None = None,
                  not_found: str = "Not found on GitHub") -> httpx.Response:
        url = self.api_url + path
        headers = {"Accept": "application/vnd.github+json", **self._auth(url, token)}
        if etag:
            headers["If-None-Match"] = etag
        try:
            r = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            raise GitHubError("Could not reach GitHub") from None
        self._track(r)
        if r.status_code == 304 or r.is_success:
            return r
        self._raise(r, not_found)
        raise AssertionError("unreachable")

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError:
            raise GitHubError("GitHub sent an invalid response") from None

    # identity -----------------------------------------------------------------------
    async def get_user(self, token: str) -> dict:
        data = self._json(await self.api("/user", token))
        return {"login": data.get("login"), "name": data.get("name"), "avatar_url": data.get("avatar_url")}

    # device flow --------------------------------------------------------------------
    async def _web_post(self, path: str, data: dict) -> dict:
        try:
            r = await self._client.post(self.web_url + path, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise GitHubError("Could not reach GitHub") from None
        if r.status_code == 404:
            raise GitHubError("GitHub does not know this OAuth client ID")
        if not r.is_success and r.status_code != 400:
            raise GitHubError(f"GitHub returned HTTP {r.status_code}")
        body = self._json(r)
        if not isinstance(body, dict):
            raise GitHubError("GitHub sent an invalid response")
        return body

    async def device_start(self, client_id: str) -> dict:
        body = await self._web_post("/login/device/code", {"client_id": client_id, "scope": "repo"})
        if "error" in body or "device_code" not in body:
            err = str(body.get("error", "unknown_error"))
            if err == "device_flow_disabled":
                raise GitHubError("Device flow is not enabled for this OAuth App")
            if err in ("incorrect_client_credentials", "unauthorized_client"):
                raise GitHubError("GitHub does not accept this OAuth client ID")
            raise GitHubError(f"GitHub refused to start the device flow ({err[:60]})")
        return body

    async def device_poll(self, client_id: str, device_code: str) -> dict:
        return await self._web_post("/login/oauth/access_token", {
            "client_id": client_id, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })

    # repos --------------------------------------------------------------------------
    async def list_repos(self, token: str) -> list[dict]:
        repos: list[dict] = []
        for page in range(1, MAX_REPO_PAGES + 1):
            r = await self.api("/user/repos", token, params={
                "per_page": 100, "page": page, "sort": "pushed", "direction": "desc",
                "affiliation": "owner,collaborator,organization_member"})
            batch = self._json(r)
            if not isinstance(batch, list):
                raise GitHubError("GitHub sent an invalid response")
            repos.extend(batch)
            if len(batch) < 100:
                break
        return repos

    async def get_repo(self, token: str, repo: str) -> dict:
        return self._json(await self.api(f"/repos/{repo}", token, not_found="Repository not found or no access"))

    async def list_branches(self, token: str, repo: str) -> list[str]:
        names: list[str] = []
        for page in range(1, MAX_REPO_PAGES + 1):
            r = await self.api(f"/repos/{repo}/branches", token, params={"per_page": 100, "page": page},
                               not_found="Repository not found or no access")
            batch = self._json(r)
            names.extend(b.get("name") for b in batch if isinstance(b, dict) and b.get("name"))
            if len(batch) < 100:
                break
        return names

    async def head(self, token: str, repo: str, branch: str, etag: str | None = None) -> dict | None:
        """Newest commit on the branch. Returns None on 304 (unchanged)."""
        r = await self.api(f"/repos/{repo}/commits/{quote(branch, safe='')}", token, etag=etag,
                           not_found="Repository or branch not found (or no access)")
        if r.status_code == 304:
            return None
        data = self._json(r)
        commit = data.get("commit") or {}
        date = (commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date")
        sha = data.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError("GitHub sent an invalid response")
        return {
            "sha": sha,
            "message": (commit.get("message") or "").split("\n", 1)[0][:300],
            "date": _norm_date(date),
            "etag": r.headers.get("etag"),
        }

    async def download_tarball(self, token: str | None, repo: str, ref: str, dest: Path, max_bytes: int) -> None:
        url = f"{self.api_url}/repos/{repo}/tarball/{quote(ref, safe='')}"
        for _ in range(5):
            try:
                async with self._client.stream("GET", url, headers=self._auth(url, token)) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        # codeload URLs carry their own short-lived credentials; our token is not
                        # forwarded unless the redirect stays on the API host.
                        url = urljoin(url, r.headers["location"])
                        continue
                    self._track(r)
                    if not r.is_success:
                        await r.aread()
                        self._raise(r, "Repository or commit not found (or no access)")
                    size = 0
                    with open(dest, "wb") as fh:
                        async for chunk in r.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise GitHubError("Repository archive is too large")
                            fh.write(chunk)
                    return
            except httpx.HTTPError:
                raise GitHubError("Download from GitHub failed") from None
        raise GitHubError("Too many redirects downloading the repository")


def _norm_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Tarball extraction and sync application (blocking; run in threads) ----------------------------


@dataclass
class Extracted:
    files: dict[str, tuple[Path, str]]  # rel -> (temp file, sha256 of content)
    skipped_links: list[str]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def extract_tarball(tar_path: Path, out_dir: Path, subdir: str, max_bytes: int, max_files: int) -> Extracted:
    """Validate and unpack a GitHub tarball into flat temp files. Never writes outside out_dir.
    Hostile paths fail the whole sync; links/devices are skipped (and reported)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, tuple[Path, str]] = {}
    skipped: list[str] = []
    total = 0
    prefix = subdir + "/" if subdir else ""
    try:
        tf = tarfile.open(tar_path, "r:*")
    except (tarfile.TarError, OSError):
        raise GitHubError("The repository archive is not a valid tarball") from None
    try:
        with tf:
            for n, m in enumerate(tf):
                if n > max_files * 3:
                    raise GitHubError("Repository has too many files")
                try:
                    full = clean_relpath(m.name)
                except UnsafePath as exc:
                    raise GitHubError(f"Repository archive contains an unsafe path: {exc.detail}") from None
                parts = full.split("/", 1)
                if len(parts) < 2:
                    continue  # the archive's single top-level folder
                rel = parts[1]
                if prefix:
                    if not rel.startswith(prefix):
                        continue
                    rel = rel[len(prefix):]
                if m.isdir():
                    continue
                if m.issym() or m.islnk():
                    skipped.append(rel)
                    continue
                if not m.isreg():
                    skipped.append(rel)  # devices, fifos, ...
                    continue
                if is_skipped(rel) or rel == ".env":
                    continue
                total += m.size
                if total > max_bytes:
                    raise GitHubError(f"Repository is too large (limit {max_bytes // (1024 * 1024)} MB)")
                if len(files) >= max_files:
                    raise GitHubError(f"Repository has too many files (limit {max_files})")
                src = tf.extractfile(m)
                if src is None:
                    continue
                tmp = out_dir / f"g{len(files):06d}"
                h = hashlib.sha256()
                with src, open(tmp, "wb") as fh:
                    while chunk := src.read(1024 * 1024):
                        h.update(chunk)
                        fh.write(chunk)
                files[rel] = (tmp, h.hexdigest())
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        if isinstance(exc, GitHubError):
            raise
        raise GitHubError("The repository archive is corrupt") from None
    if subdir and not files:
        raise GitHubError(f"Subfolder {subdir!r} not found in the repository (or it is empty)")
    return Extracted(files, skipped)


MAX_KEEP_PATTERNS = 50
MAX_BACKUP_SETS = 10


def compile_keep(patterns: list[str]) -> re.Pattern | None:
    """Glob patterns (posix, ``*`` within a segment, ``**`` across segments, ``?``) -> one regex.
    A pattern without wildcards also matches everything below it (``data`` == ``data`` + ``data/**``);
    a trailing ``/`` means the folder's contents."""
    parts = []
    for pat in patterns:
        p = pat.strip()
        if p.endswith("/"):
            p += "**"
        rx, i = "", 0
        while i < len(p):
            if p.startswith("**/", i):
                rx += "(?:.*/)?"
                i += 3
            elif p.startswith("**", i):
                rx += ".*"
                i += 2
            elif p[i] == "*":
                rx += "[^/]*"
                i += 1
            elif p[i] == "?":
                rx += "[^/]"
                i += 1
            else:
                rx += re.escape(p[i])
                i += 1
        if not any(c in p for c in "*?"):
            rx += "(?:/.*)?"
        parts.append(rx)
    return re.compile("^(?:" + "|".join(parts) + ")$") if parts else None


def validate_keep_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError(400, "keep_paths must be a list of strings")
    out: list[str] = []
    for raw in value:
        p = raw.strip()
        if p.startswith("./"):
            p = p[2:]
        if not p:
            continue
        if len(p) > 200 or p.startswith("/") or "\\" in p or ".." in p.split("/") or any(ord(c) < 32 for c in p):
            raise ApiError(400, f"Invalid keep_paths pattern: {raw[:60]!r}")
        if p not in out:
            out.append(p)
    if len(out) > MAX_KEEP_PATTERNS:
        raise ApiError(400, f"At most {MAX_KEEP_PATTERNS} keep_paths patterns")
    compile_keep(out)
    return out


def _new_backup_dir(backups_root: Path) -> Path:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d, n = backups_root / stamp, 1
    while d.exists():
        n += 1
        d = backups_root / f"{stamp}-{n}"
    return d


def prune_backups(backups_root: Path, keep: int = MAX_BACKUP_SETS) -> None:
    if not backups_root.is_dir():
        return
    sets = sorted((p for p in backups_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in sets[:-keep] if len(sets) > keep else []:
        shutil.rmtree(old, ignore_errors=True)


@dataclass
class SyncResult:
    manifest: dict[str, str]
    messages: list[str]
    written: int
    deleted: int
    kept: int
    backup_dir: Path | None


def plan_and_apply_sync(src: Path, ex: Extracted, manifest: dict[str, str], keep_paths: list[str] | None = None,
                        backups_root: Path | None = None) -> SyncResult:
    """Apply the sync rules. All checks run before the first write, so a conflict leaves src untouched.
    Locally modified files that get overwritten are copied to ``backups_root/<UTC stamp>/<path>`` first."""
    src.mkdir(parents=True, exist_ok=True)
    keep_rx = compile_keep(keep_paths or [])
    messages: list[str] = []
    writes: list[tuple[str, Path, Path, bool]] = []  # rel, tmp, dest, needs backup
    new_manifest: dict[str, str] = {}
    kept = 0
    for rel, (tmp, up_hash) in sorted(ex.files.items()):
        prev = manifest.get(rel)
        dest = resolve_inside(src, rel)
        if keep_rx is not None and keep_rx.match(rel) and os.path.lexists(dest):
            if prev is not None:
                new_manifest[rel] = prev  # baseline unchanged: the file is the user's while kept
            if prev != up_hash:
                kept += 1
            continue
        new_manifest[rel] = up_hash
        if prev == up_hash:
            continue  # upstream unchanged since last sync: local copy (maybe runtime-modified) wins
        parts = rel.split("/")
        for i in range(1, len(parts)):
            p = Path(src, *parts[:i])
            if os.path.lexists(p) and not p.is_dir():
                raise GitHubError(f"Cannot write {rel}: {'/'.join(parts[:i])} is a file locally")
        if os.path.lexists(dest) and not dest.is_file():
            raise GitHubError(f"Cannot write {rel}: a folder with that name exists locally")
        backup = False
        if dest.is_file():
            local = _sha256_file(dest)
            if local != up_hash and local != prev:
                messages.append(f"overwrote locally modified {rel}")
                backup = True
        writes.append((rel, tmp, dest, backup))
    deletes: list[Path] = []
    for rel, prev in sorted(manifest.items()):
        if rel in ex.files:
            continue
        try:
            dest = resolve_inside(src, rel)
        except UnsafePath:
            continue
        if keep_rx is not None and keep_rx.match(rel):
            continue  # kept: never deleted; it is now a local-only file
        if dest.is_file() and not dest.is_symlink():
            if _sha256_file(dest) == prev:
                deletes.append(dest)
            else:
                messages.append(f"kept locally modified {rel} (removed upstream)")

    backup_dir = None
    to_backup = [(rel, dest) for rel, _, dest, b in writes if b]
    if to_backup and backups_root is not None:
        backup_dir = _new_backup_dir(backups_root)
        for rel, dest in to_backup:
            target = backup_dir.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, target)
        prune_backups(backups_root)
    for rel, tmp, dest, _ in writes:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resolve_inside(src, rel)
        try:
            os.replace(tmp, dest)
        except OSError:
            shutil.copyfile(tmp, dest)
    src_real = os.path.realpath(src)
    for dest in deletes:
        try:
            dest.unlink()
        except OSError:
            continue
        parent = dest.parent  # prune folders the deletion left empty
        while os.path.realpath(parent) != src_real and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return SyncResult(new_manifest, messages, len(writes), len(deletes), kept, backup_dir)


# Service ---------------------------------------------------------------------------------------


class GitHubService:
    def __init__(self, settings: Settings, db: Database, crypto: Crypto, supervisor, bots,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.db = db
        self.crypto = crypto
        self.supervisor = supervisor
        self.bots = bots
        self.transport = transport
        self.client: GitHubClient | None = None
        self.syncing: set[str] = set()
        self.rate_limited_until = 0.0
        self._device: dict | None = None
        self._device_lock = asyncio.Lock()
        self._repo_cache: tuple[float, list[dict]] | None = None
        self._failed_sha: dict[str, str] = {}
        self._poll_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        if settings.github_client_id and self.db.kv_get(KV_CLIENT_ID) is None:
            self.db.kv_set(KV_CLIENT_ID, settings.github_client_id)

    # lifecycle ----------------------------------------------------------------------
    async def startup(self) -> None:
        self.client = GitHubClient(self.settings.github_api_url, self.settings.github_web_url,
                                   transport=self.transport, timeout=self.settings.github_timeout)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [t for t in [self._poll_task, *self._tasks] if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # account --------------------------------------------------------------------------
    def token(self) -> str | None:
        enc = self.db.kv_get(KV_TOKEN)
        if not enc:
            return None
        try:
            return self.crypto.decrypt(enc.encode("ascii"))
        except InvalidToken:
            return None

    def require_token(self) -> str:
        tok = self.token()
        if not tok:
            raise ApiError(409, NOT_CONNECTED)
        return tok

    def poll_interval(self) -> int:
        raw = self.db.kv_get(KV_POLL)
        return int(raw) if raw and raw.isdigit() else DEFAULT_POLL_INTERVAL

    def state_json(self) -> dict:
        connected = self.token() is not None
        return {
            "connected": connected,
            "login": self.db.kv_get(KV_LOGIN) if connected else None,
            "name": self.db.kv_get(KV_NAME) if connected else None,
            "avatar_url": self.db.kv_get(KV_AVATAR) if connected else None,
            "method": self.db.kv_get(KV_METHOD) if connected else None,
            "client_id": self.db.kv_get(KV_CLIENT_ID),
            "poll_interval_seconds": self.poll_interval(),
            "rate_limit_remaining": self.client.rate_remaining if self.client else None,
        }

    def update_settings(self, data: dict) -> dict:
        if "client_id" in data:
            cid = data["client_id"]
            if cid is None or (isinstance(cid, str) and cid.strip() == ""):
                self.db.kv_delete(KV_CLIENT_ID)
            elif isinstance(cid, str) and CLIENT_ID_RE.match(cid.strip()):
                self.db.kv_set(KV_CLIENT_ID, cid.strip())
            else:
                raise ApiError(400, "client_id looks invalid")
        if "poll_interval_seconds" in data:
            val = data["poll_interval_seconds"]
            if isinstance(val, bool) or not isinstance(val, int) or not 30 <= val <= 3600:
                raise ApiError(400, "poll_interval_seconds must be an integer between 30 and 3600")
            self.db.kv_set(KV_POLL, str(val))
        return self.state_json()

    async def _store_login(self, token: str, method: str) -> None:
        assert self.client is not None
        try:
            user = await self.client.get_user(token)
        except TokenRejected:
            raise ApiError(400, "GitHub rejected this token") from None
        except GitHubError as exc:
            raise ApiError(502, exc.message) from None
        self.db.kv_set(KV_TOKEN, self.crypto.encrypt(token).decode("ascii"))
        self.db.kv_set(KV_METHOD, method)
        for key, value in ((KV_LOGIN, user["login"]), (KV_NAME, user["name"]), (KV_AVATAR, user["avatar_url"])):
            if value:
                self.db.kv_set(key, str(value))
            else:
                self.db.kv_delete(key)
        self._repo_cache = None
        self.rate_limited_until = 0.0
        self._failed_sha.clear()
        self._refresh_not_connected_errors()

    async def connect_token(self, token: Any) -> dict:
        if not isinstance(token, str) or not token.strip():
            raise ApiError(400, "token is required")
        token = token.strip()
        if len(token) > 512 or any(c.isspace() for c in token):
            raise ApiError(400, "That does not look like a GitHub token")
        await self._store_login(token, "token")
        return self.state_json()

    def disconnect(self) -> None:
        for key in (KV_TOKEN, KV_METHOD, KV_LOGIN, KV_NAME, KV_AVATAR):
            self.db.kv_delete(key)
        self._device = None
        self._repo_cache = None
        for src in self.db.list_sources():
            self._set_error(src["bot_id"], NOT_CONNECTED)

    def _refresh_not_connected_errors(self) -> None:
        for src in self.db.list_sources():
            if src["last_error"] == NOT_CONNECTED:
                self._set_error(src["bot_id"], None)

    # device flow ----------------------------------------------------------------------
    async def device_start(self) -> dict:
        client_id = self.db.kv_get(KV_CLIENT_ID)
        if not client_id:
            raise ApiError(409, "No GitHub OAuth client ID is configured")
        assert self.client is not None
        try:
            body = await self.client.device_start(client_id)
        except GitHubError as exc:
            raise ApiError(502, exc.message) from None
        raw_interval = body.get("interval")
        interval = int(raw_interval) if isinstance(raw_interval, int) or str(raw_interval).isdigit() else 5
        expires_in = int(body.get("expires_in") or 900)
        now = time.time()
        self._device = {
            "client_id": client_id, "device_code": body["device_code"], "interval": interval,
            "expires_at": now + expires_in, "next_poll_at": now + interval,
        }
        return {"user_code": body.get("user_code"), "verification_uri": body.get("verification_uri"),
                "expires_in": expires_in, "interval": interval}

    async def device_poll(self) -> dict:
        async with self._device_lock:
            d = self._device
            if d is None:
                return {"status": "none"}
            now = time.time()
            if now >= d["expires_at"]:
                self._device = None
                return {"status": "expired"}
            if now < d["next_poll_at"]:
                return {"status": "pending"}
            assert self.client is not None
            try:
                body = await self.client.device_poll(d["client_id"], d["device_code"])
            except GitHubError:
                d["next_poll_at"] = time.time() + d["interval"]
                return {"status": "pending"}
            if body.get("access_token"):
                self._device = None
                await self._store_login(str(body["access_token"]), "device")
                return {"status": "connected"}
            err = body.get("error")
            if err == "authorization_pending":
                d["next_poll_at"] = time.time() + d["interval"]
                return {"status": "pending"}
            if err == "slow_down":
                new = body.get("interval")
                d["interval"] = int(new) if isinstance(new, int) or (isinstance(new, str) and new.isdigit()) \
                    else d["interval"] + 5
                d["next_poll_at"] = time.time() + d["interval"]
                return {"status": "pending"}
            if err == "expired_token":
                self._device = None
                return {"status": "expired"}
            if err == "access_denied":
                self._device = None
                return {"status": "denied"}
            self._device = None
            raise ApiError(502, f"GitHub device flow failed ({str(err)[:60]})")

    # repos ----------------------------------------------------------------------------
    async def _call(self, coro):
        """Map client errors on interactive endpoints onto HTTP errors."""
        if time.time() < self.rate_limited_until:
            raise ApiError(429, "Rate limited by GitHub until " + (iso(self.rate_limited_until) or "later"))
        try:
            return await coro
        except TokenRejected as exc:
            raise ApiError(409, exc.message) from None
        except RateLimited as exc:
            self.rate_limited_until = exc.until
            raise ApiError(429, exc.message) from None
        except NotFound as exc:
            raise ApiError(404, exc.message) from None
        except GitHubError as exc:
            raise ApiError(502, exc.message) from None

    async def list_repos(self, q: str, page: int) -> dict:
        token = self.require_token()
        assert self.client is not None
        cache = self._repo_cache
        if cache is None or time.monotonic() - cache[0] > REPO_CACHE_SECONDS:
            raw = await self._call(self.client.list_repos(token))
            repos = []
            for r in raw:
                if not isinstance(r, dict) or not r.get("full_name"):
                    continue
                repos.append({
                    "full_name": r["full_name"], "name": r.get("name"),
                    "owner": (r.get("owner") or {}).get("login"), "private": bool(r.get("private")),
                    "default_branch": r.get("default_branch"), "description": r.get("description"),
                    "language": r.get("language"), "pushed_at": _norm_date(r.get("pushed_at")),
                    "html_url": r.get("html_url"),
                })
            repos.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
            self._repo_cache = cache = (time.monotonic(), repos)
        items = cache[1]
        if q:
            ql = q.lower()
            items = [r for r in items if ql in (r["name"] or "").lower() or ql in r["full_name"].lower()]
        page = max(1, page)
        start = (page - 1) * REPOS_PAGE_SIZE
        return {"repos": items[start:start + REPOS_PAGE_SIZE], "has_more": len(items) > start + REPOS_PAGE_SIZE}

    async def branches(self, owner: str, repo: str) -> dict:
        if not NAME_RE.match(owner) or not NAME_RE.match(repo):
            raise ApiError(400, "Invalid repository name")
        token = self.require_token()
        assert self.client is not None
        full = f"{owner}/{repo}"
        info = await self._call(self.client.get_repo(token, full))
        names = await self._call(self.client.list_branches(token, full))
        default = info.get("default_branch")
        names = sorted(set(names), key=lambda n: (n != default, n.lower()))
        return {"branches": names, "default_branch": default}

    # source JSON ----------------------------------------------------------------------
    def source_json(self, bot_id: str) -> dict:
        src = self.db.get_source(bot_id)
        if src is None:
            return {"type": "upload"}
        return {
            "type": "github",
            "repo": src["repo"],
            "branch": src["branch"],
            "subdir": src["subdir"] or "",
            "auto_update": bool(src["auto_update"]),
            "keep_paths": json.loads(src["keep_paths"] or "[]"),
            "commit_sha": src["commit_sha"],
            "commit_message": src["commit_message"],
            "commit_date": src["commit_date"],
            "latest_sha": src["latest_sha"],
            "update_available": bool(src["latest_sha"]) and src["latest_sha"] != src["commit_sha"],
            "last_checked_at": iso(src["last_checked_at"]),
            "last_synced_at": iso(src["last_synced_at"]),
            "syncing": bot_id in self.syncing,
            "last_error": src["last_error"],
        }

    def _set_error(self, bot_id: str, message: str | None, log_line: bool = True) -> None:
        src = self.db.get_source(bot_id)
        if src is None:
            return
        if src["last_error"] != message:
            self.db.update_source(bot_id, {"last_error": message})
            if message and log_line and bot_id in self.supervisor.runtimes:
                self.supervisor.runtime(bot_id).system(f"GitHub: {message}")
            self.bots.publish(bot_id)

    # link / unlink --------------------------------------------------------------------
    async def set_source(self, bot_id: str, data: dict) -> dict:
        self.bots.get_row(bot_id)
        kind = data.get("type")
        if kind == "upload":
            async def unlink() -> None:
                self.bots.get_row(bot_id)
                self.db.delete_source(bot_id)
                self._failed_sha.pop(bot_id, None)
            await self.supervisor.serialized(bot_id, unlink)
            self.bots.publish(bot_id)
            return self.bots.get_json(bot_id)
        if kind != "github":
            raise ApiError(400, "type must be 'github' or 'upload'")
        repo = validate_repo(data.get("repo"))
        subdir = validate_subdir(data.get("subdir"))
        auto = data.get("auto_update", True)
        if not isinstance(auto, bool):
            raise ApiError(400, "auto_update must be a boolean")
        keep_given = "keep_paths" in data
        keep_paths = validate_keep_paths(data.get("keep_paths"))
        branch_raw = data.get("branch")
        if branch_raw in (None, ""):
            token = self.require_token()
            assert self.client is not None
            branch = validate_branch((await self._call(self.client.get_repo(token, repo))).get("default_branch"))
        else:
            branch = validate_branch(branch_raw)

        async def link() -> None:
            self.bots.get_row(bot_id)
            cur = self.db.get_source(bot_id)
            fields: dict[str, Any] = {"repo": repo, "branch": branch, "subdir": subdir, "auto_update": 1 if auto else 0}
            if keep_given or cur is None:
                fields["keep_paths"] = json.dumps(keep_paths)
            if cur is None or (cur["repo"], cur["branch"], cur["subdir"]) != (repo, branch, subdir):
                fields.update(latest_sha=None, etag=None, last_error=None, last_checked_at=None)
                self._failed_sha.pop(bot_id, None)
            if not self.token():
                fields["last_error"] = NOT_CONNECTED  # say right away why nothing will happen
            self.db.upsert_source(bot_id, fields)

        await self.supervisor.serialized(bot_id, link)
        self.bots.publish(bot_id)
        if self.token():
            self._spawn(self._quiet_check(bot_id))
        return self.bots.get_json(bot_id)

    async def _quiet_check(self, bot_id: str) -> None:
        try:
            await self.check(bot_id)
        except Exception:  # noqa: BLE001 - background refresh only
            pass

    # check ------------------------------------------------------------------------------
    async def check(self, bot_id: str, use_etag: bool = False) -> dict | None:
        """Refresh latest_sha. Returns the head info, or None if unchanged (304) / failed.
        Errors are stored in source.last_error (never raised) except 'not connected'."""
        src = self.db.get_source(bot_id)
        if src is None:
            raise ApiError(409, "This bot is not linked to a GitHub repository")
        token = self.token()
        if not token:
            self._set_error(bot_id, NOT_CONNECTED)
            raise ApiError(409, NOT_CONNECTED)
        assert self.client is not None
        if time.time() < self.rate_limited_until:
            self._set_error(bot_id, "Rate limited by GitHub until " + (iso(self.rate_limited_until) or "later"))
            return None
        try:
            head = await self.client.head(token, src["repo"], src["branch"], etag=src["etag"] if use_etag else None)
        except RateLimited as exc:
            self.rate_limited_until = exc.until
            self._set_error(bot_id, exc.message)
            raise
        except GitHubError as exc:
            self.db.update_source(bot_id, {"last_checked_at": time.time()})
            self._set_error(bot_id, exc.message)
            return None
        fields: dict[str, Any] = {"last_checked_at": time.time()}
        if head is not None:
            fields.update(latest_sha=head["sha"], etag=head["etag"])
        if self.db.get_source(bot_id) is None:
            return None
        self.db.update_source(bot_id, fields)
        self._set_error(bot_id, None)
        self.bots.publish(bot_id)
        return head

    # sync -------------------------------------------------------------------------------
    def start_sync(self, bot_id: str) -> None:
        """Kick off a sync in the background (returns immediately)."""
        if self.db.get_source(bot_id) is None:
            raise ApiError(409, "This bot is not linked to a GitHub repository")
        self.require_token()
        if bot_id in self.syncing:
            return
        self.syncing.add(bot_id)
        self.bots.publish(bot_id)
        self._spawn(self.sync(bot_id, already_marked=True))

    async def sync(self, bot_id: str, already_marked: bool = False, reason: str = "pulled update") -> bool:
        """Pull the branch head into src under the bot lock. Returns True if new code was applied."""
        if not already_marked:
            if bot_id in self.syncing:
                return False
            self.syncing.add(bot_id)
            self.bots.publish(bot_id)
        try:
            return await self.supervisor.serialized(bot_id, lambda: self._sync_locked(bot_id, reason))
        finally:
            self.syncing.discard(bot_id)
            self.bots.publish(bot_id)

    async def _fetch(self, token: str, repo: str, sha: str, subdir: str, work: Path) -> Extracted:
        assert self.client is not None
        tar_path = work / "repo.tar.gz"
        await self.client.download_tarball(token, repo, sha, tar_path, self.settings.max_upload_bytes)
        return await asyncio.to_thread(extract_tarball, tar_path, work / "files", subdir,
                                       self.settings.max_upload_bytes, self.settings.max_upload_files)

    def _work_dir(self) -> Path:
        work = self.settings.tmp_dir / f"github-{secrets.token_hex(8)}"
        work.mkdir(parents=True)
        return work

    async def _sync_locked(self, bot_id: str, reason: str) -> bool:
        if self._closing or self.db.get_bot(bot_id) is None:
            return False
        src_row = self.db.get_source(bot_id)
        if src_row is None:
            return False
        rt = self.supervisor.runtime(bot_id)
        token = self.token()
        if not token:
            self._set_error(bot_id, NOT_CONNECTED)
            return False
        assert self.client is not None
        repo, branch, subdir = src_row["repo"], src_row["branch"], src_row["subdir"] or ""
        work = self._work_dir()
        tried = src_row["latest_sha"] or ""
        try:
            try:
                head = await self.client.head(token, repo, branch)
                assert head is not None
                tried = head["sha"]
                rt.system(f"Pulling {repo}@{branch} ({head['sha'][:7]}: {head['message'][:80]})…")
                ex = await self._fetch(token, repo, head["sha"], subdir, work)
                manifest = json.loads(src_row["manifest"] or "{}")
                src_dir = self.settings.src_dir(bot_id)
                result = await asyncio.to_thread(
                    plan_and_apply_sync, src_dir, ex, manifest, json.loads(src_row["keep_paths"] or "[]"),
                    self.settings.bot_dir(bot_id) / "backups")
            except RateLimited as exc:
                self.rate_limited_until = exc.until
                raise
            except (GitHubError, UnsafePath, ApiError) as exc:
                msg = exc.message if isinstance(exc, GitHubError) else exc.detail
                self._failed_sha[bot_id] = tried
                fields = {"last_error": msg}
                if tried:
                    fields["latest_sha"] = tried
                self.db.update_source(bot_id, fields)
                rt.system(f"Sync failed: {msg}")
                self.bots.publish(bot_id)
                return False
        except RateLimited as exc:
            self.db.update_source(bot_id, {"last_error": exc.message})
            rt.system(f"Sync failed: {exc.message}")
            self.bots.publish(bot_id)
            return False
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)

        messages = list(result.messages)
        for line in ex.skipped_links[:20]:
            messages.append(f"skipped link/special file {line}")
        if result.backup_dir is not None:
            rel_backup = result.backup_dir.relative_to(self.settings.bot_dir(bot_id)).as_posix()
            messages.append(f"Backed up the previous version of locally modified files to {rel_backup}/ "
                            f"(in the bot's data folder, outside src)")
        if result.kept:
            messages.append(f"left {result.kept} file(s) alone because they match keep_paths")
        for m in messages:
            rt.system(m)
        now = time.time()
        self.db.update_source(bot_id, {
            "commit_sha": head["sha"], "commit_message": head["message"], "commit_date": head["date"],
            "latest_sha": head["sha"], "etag": head["etag"], "last_synced_at": now, "last_checked_at": now,
            "last_error": None, "manifest": json.dumps(result.manifest, separators=(",", ":")),
        })
        self._failed_sha.pop(bot_id, None)
        self.bots.after_code_change(bot_id)
        rt.system(f"Synced {head['sha'][:7]}: {result.written} file(s) written, {result.deleted} removed")
        await self.supervisor.after_update_locked(bot_id, f"update {head['sha'][:7]} from GitHub")
        self.bots.publish(bot_id)
        return True

    # create from repo -------------------------------------------------------------------
    async def create_bot(self, data: dict) -> dict:
        from .bots import choose_entrypoint, detect_entrypoints, validate_name

        token = self.require_token()
        assert self.client is not None
        name = validate_name(data.get("name"))
        description = data.get("description") or ""
        if not isinstance(description, str) or len(description) > 2000:
            raise ApiError(400, "Description must be a string of at most 2000 characters")
        repo = validate_repo(data.get("repo"))
        subdir = validate_subdir(data.get("subdir"))
        auto = data.get("auto_update", True)
        if not isinstance(auto, bool):
            raise ApiError(400, "auto_update must be a boolean")
        keep_paths = validate_keep_paths(data.get("keep_paths"))
        entry_override = data.get("entrypoint") or ""
        if not isinstance(entry_override, str):
            raise ApiError(400, "entrypoint must be a string")
        if self.db.bot_name_taken(name):
            raise ApiError(409, f"A bot named {name!r} already exists")
        branch_raw = data.get("branch")
        if branch_raw in (None, ""):
            info = await self._call(self.client.get_repo(token, repo))
            branch = validate_branch(info.get("default_branch"))
        else:
            branch = validate_branch(branch_raw)
        head = await self._call(self.client.head(token, repo, branch))
        work = self._work_dir()
        try:
            try:
                ex = await self._fetch(token, repo, head["sha"], subdir, work)
            except RateLimited as exc:
                self.rate_limited_until = exc.until
                raise ApiError(429, exc.message) from None
            except NotFound as exc:
                raise ApiError(404, exc.message) from None
            except GitHubError as exc:
                raise ApiError(400 if "archive" in exc.message or "Subfolder" in exc.message
                               or "too" in exc.message else 502, exc.message) from None
            if not ex.files:
                raise ApiError(400, "The repository has no files to deploy")
            bot_id = self.bots.new_id()
            src_dir = self.settings.src_dir(bot_id)
            try:
                manifest = (await asyncio.to_thread(plan_and_apply_sync, src_dir, ex, {}, keep_paths)).manifest
                self.settings.logs_dir(bot_id).mkdir(parents=True, exist_ok=True)
                detected = detect_entrypoints(src_dir)
                if entry_override.strip():
                    entry = self.bots.validate_entrypoint(bot_id, entry_override.strip())
                else:
                    entry = choose_entrypoint(None, detected)
                now = time.time()
                self.db.insert_bot({
                    "id": bot_id, "name": name, "description": description, "entrypoint": entry, "args": "",
                    "extra_packages": "", "restart_policy": "on-failure", "autostart": 1,
                    "desired_state": "stopped", "deps_fingerprint": None, "created_at": now, "updated_at": now,
                })
                self.db.upsert_source(bot_id, {
                    "repo": repo, "branch": branch, "subdir": subdir, "auto_update": 1 if auto else 0,
                    "commit_sha": head["sha"], "commit_message": head["message"], "commit_date": head["date"],
                    "latest_sha": head["sha"], "etag": head["etag"], "last_synced_at": now, "last_checked_at": now,
                    "last_error": None, "manifest": json.dumps(manifest, separators=(",", ":")),
                    "keep_paths": json.dumps(keep_paths),
                })
            except BaseException as exc:
                if self.db.get_bot(bot_id) is not None:
                    self.db.delete_bot(bot_id)
                await asyncio.to_thread(shutil.rmtree, self.settings.bot_dir(bot_id), True)
                if isinstance(exc, GitHubError):
                    raise ApiError(409, exc.message) from None
                if isinstance(exc, __import__("sqlite3").IntegrityError):
                    raise ApiError(409, f"A bot named {name!r} already exists") from None
                raise
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)
        rt = self.supervisor.runtime(bot_id)
        rt.system(f"Created from {repo}@{branch} ({head['sha'][:7]}: {head['message'][:80]})")
        for link in ex.skipped_links[:20]:
            rt.system(f"skipped link/special file {link}")
        self.bots.publish(bot_id)
        return {"bot": self.bots.get_json(bot_id), "imported_env_keys": [], "skipped_env_keys": [],
                "detected_entrypoints": detected}

    # poller -----------------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        await asyncio.sleep(self.settings.github_first_poll_delay)
        while not self._closing:
            started = time.monotonic()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the poller die
                log.exception("GitHub poll failed")
            while not self._closing:
                wait_until = max(started + self.poll_interval(), self.rate_limited_until - time.time() + time.monotonic())
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, 1.0))

    async def poll_once(self) -> None:
        """Check every linked bot once; auto-sync where the head moved."""
        sources = self.db.list_sources()
        if not sources:
            return
        if not self.token():
            for src in sources:
                self._set_error(src["bot_id"], NOT_CONNECTED)
            return
        for src in sources:
            if self._closing:
                return
            if time.time() < self.rate_limited_until:
                return
            bot_id = src["bot_id"]
            try:
                await self.check(bot_id, use_etag=True)
            except RateLimited:
                return
            except ApiError:
                continue
            cur = self.db.get_source(bot_id)
            if cur is None or not cur["auto_update"] or not cur["latest_sha"]:
                continue
            if not cur["commit_sha"]:
                continue  # never pulled since linking: only an explicit "Pull now" does the first sync
            if cur["latest_sha"] == cur["commit_sha"] or bot_id in self.syncing:
                continue
            if self._failed_sha.get(bot_id) == cur["latest_sha"]:
                continue  # this head already failed to sync; wait for a new commit
            await self.sync(bot_id, reason="auto-update")
