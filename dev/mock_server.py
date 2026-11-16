#!/usr/bin/env python3
"""BotDock MOCK API server -- a throwaway dev tool for working on the frontend.

NOT the product. Stdlib only. Serves botdock/static/ and fakes the API from SPEC.md with
in-memory state (simulated process lifecycle, SSE events, log streams, uploads, files, secrets).

    python dev/mock_server.py                 # http://127.0.0.1:8099, password "password", seeded bots
    python dev/mock_server.py --setup         # start in first-run setup state
    python dev/mock_server.py --empty         # no bots
    python dev/mock_server.py --chatty 200    # first bot logs 200 lines/s (console perf test)
    python dev/mock_server.py --throttle 500  # read uploads at ~500 KB/s (see the progress bar)

Extra dev-only endpoints: POST /__mock/expire  -> invalidates all sessions (test 401 handling).
                         POST /__mock/push?repo=owner/name -> new commit on that repo's default branch.
GitHub: --github starts connected. Device flow: pending for ~3 polls, then connected. Client ID
"expire" / "deny" make the device flow end expired / denied. Token "bad" is rejected.
--avatar-offline points avatar_url at an unreachable host (tests the initials fallback).
--setup also requires a one-time setup code (printed at start). --allowed-hosts a,b rejects other Host headers.
Setting extra_packages to contain FAIL_INSTALL makes the next install fail.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import json
import queue
import random
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

STATIC = Path(__file__).resolve().parent.parent / "botdock" / "static"
CSP = "default-src 'self'; img-src 'self' data: https://avatars.githubusercontent.com; style-src 'self' 'unsafe-inline'"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
ENTRY_PREF = ["main.py", "bot.py", "app.py", "run.py", "start.py", "__main__.py"]
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARGS = None
LOCK = threading.RLock()
STARTED = time.time()


def iso(dt: datetime | None = None, ms: bool = False) -> str:
    dt = dt or datetime.now(timezone.utc)
    s = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return (s[:-3] if ms else s[:-7]) + "Z"


def iso_ts(t: float | None, ms: bool = False) -> str | None:
    return None if t is None else iso(datetime.fromtimestamp(t, timezone.utc), ms)


class ApiErr(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- state
class Hub:
    def __init__(self):
        self.subs: list[queue.Queue] = []

    def add(self) -> queue.Queue:
        q = queue.Queue(maxsize=20000)
        with LOCK:
            self.subs.append(q)
        return q

    def remove(self, q):
        with LOCK:
            if q in self.subs:
                self.subs.remove(q)

    def send(self, msg: str):
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def sse(event: str | None, data) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


EVENTS = Hub()
STATE = {"password": "password", "sessions": set(), "fails": [], "setup_code": None}
BOTS: dict[str, "Bot"] = {}

SAMPLE_LINES = [
    "[INFO] discord.gateway: Shard ID None has sent the HEARTBEAT payload.",
    "[INFO] cogs.utility: /ping used by user#{n} in guild 1180{n}",
    "[DEBUG] aiohttp: GET https://discord.com/api/v10/users/@me 200",
    "[WARN] ratelimit: bucket POST /channels/:id/messages exhausted, retrying in 0.{n}s",
    "Processed {n} messages this minute",
    "<b>not html</b> &amp; <img src=x onerror=alert(1)> -- log text must render literally",
    "\x1b[32mINFO\x1b[0m colourised line with ANSI escapes {n}",
    "A very long line: " + "lorem ipsum dolor sit amet " * 12,
]


class Bot:
    def __init__(self, name, description="", entrypoint="main.py", files=None, env=None):
        self.id = "b_" + secrets.token_hex(4)
        self.name, self.description, self.entrypoint = name, description, entrypoint
        self.args, self.extra_packages = "", ""
        self.restart_policy, self.autostart = "on-failure", True
        self.created_at = self.updated_at = iso()
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: set[str] = set()
        self.env: dict[str, tuple[str, str]] = {k: (v, iso()) for k, v in (env or {}).items()}
        self.needs_restart = False
        self.state, self.pid, self.started_at, self.exit_code = "stopped", None, None, None
        self.restart_count, self.next_restart_at, self.last_error = 0, None, None
        self.phase_until = 0.0
        self.flaky_after = None  # seconds of uptime before a simulated crash
        self.logs: list[dict] = []
        self.seq = 0
        self.log_hub = Hub()
        self.source = {"type": "upload"}

    def all_dirs(self) -> set[str]:
        out = set(self.dirs)
        for p in self.files:
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return out

    def json(self) -> dict:
        running = self.state == "running"
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "entrypoint": self.entrypoint, "args": self.args, "extra_packages": self.extra_packages,
            "restart_policy": self.restart_policy, "autostart": self.autostart,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "env_count": len(self.env), "file_count": len(self.files),
            "size_bytes": sum(len(v) for v in self.files.values()),
            "has_requirements": "requirements.txt" in self.files,
            "needs_restart": self.needs_restart and self.state in ("running", "starting"),
            "runtime": {
                "state": self.state, "pid": self.pid if self.state not in ("stopped", "crashed") else None,
                "started_at": iso_ts(self.started_at) if running else None,
                "exit_code": self.exit_code, "restart_count": self.restart_count,
                "next_restart_at": iso_ts(self.next_restart_at),
                "cpu_percent": round(random.uniform(0.1, 3.5), 1) if running else None,
                "memory_bytes": random.randint(40_000_000, 52_000_000) if running else None,
                "last_error": self.last_error,
            },
            "source": dict(self.source),
        }

    def changed(self):
        self.updated_at = iso()
        EVENTS.send(sse("bot", self.json()))

    def log(self, text: str, stream: str = "stdout"):
        self.seq += 1
        line = {"seq": self.seq, "ts": iso(ms=True), "stream": stream, "text": text}
        self.logs.append(line)
        if len(self.logs) > 5000:
            del self.logs[: len(self.logs) - 5000]
        self.log_hub.send(sse(None, line))

    def mark_dirty(self):
        if self.state in ("running", "starting"):
            self.needs_restart = True
        self.changed()

    def start(self):
        if not self.entrypoint:
            raise ApiErr(409, "No entrypoint set")
        if self.state in ("running", "starting", "installing"):
            raise ApiErr(409, "Bot is already running")
        self.restart_count, self.next_restart_at, self.last_error = 0, None, None
        self._begin(install=True)

    def _begin(self, install=False):
        self.needs_restart = False
        if install and "requirements.txt" in self.files:
            self.state, self.phase_until = "installing", time.time() + 3
            self.log("Installing dependencies…", "system")
            self.log("Collecting discord.py>=2.3", "system")
        else:
            self.state, self.phase_until = "starting", time.time() + 3
            self.pid = random.randint(1000, 30000)
            self.log(f"Starting {self.entrypoint}", "system")
        self.changed()

    def stop(self):
        if self.state == "stopped":
            raise ApiErr(409, "Bot is not running")
        if self.state == "crashed" and not self.next_restart_at:
            self.state, self.last_error = "stopped", None
        else:
            self.state, self.phase_until = "stopping", time.time() + 1.5
            self.next_restart_at = None
            self.log("Stopping (SIGTERM)…", "system")
        self.changed()

    def tick(self, now: float, first: bool):
        if self.source.get("syncing") and now >= getattr(self, "sync_until", 0):
            gh_finish_sync(self)
        if self.state == "installing" and now >= self.phase_until:
            if "FAIL_INSTALL" in self.extra_packages:
                self.state, self.last_error, self.exit_code = "crashed", "Dependency install failed (exit 1)", 1
                self.log("ERROR: No matching distribution found for FAIL_INSTALL", "system")
                self.changed()
                return
            self.log("Successfully installed discord.py-2.4.0 aiohttp-3.10.5", "system")
            self.state, self.phase_until = "starting", now + 3
            self.pid = random.randint(1000, 30000)
            self.log(f"Starting {self.entrypoint}", "system")
            self.changed()
        elif self.state == "starting" and now >= self.phase_until:
            self.state, self.started_at = "running", now - 3
            self.log(f"Logged in as {self.name}#1234")
            self.changed()
        elif self.state == "stopping" and now >= self.phase_until:
            self.state, self.pid, self.started_at, self.exit_code = "stopped", None, None, 0
            self.log("Process exited with code 0", "system")
            self.changed()
        elif self.state == "crashed" and self.next_restart_at and now >= self.next_restart_at:
            self.next_restart_at = None
            self.restart_count += 1
            self._begin()
        elif self.state == "running":
            n = ARGS.chatty if (ARGS.chatty and first) else 1
            for _ in range(n):
                self.log(random.choice(SAMPLE_LINES).format(n=random.randint(1, 999)),
                         "stderr" if random.random() < 0.08 else "stdout")
            if self.flaky_after and now - self.started_at > self.flaky_after:
                self.log("Traceback (most recent call last):", "stderr")
                self.log('  File "main.py", line 42, in on_message', "stderr")
                self.log("KeyError: 'guild_id'", "stderr")
                self.log("Process exited with code 1", "system")
                self.log("Restarting in 8s", "system")
                self.state, self.started_at, self.exit_code = "crashed", None, 1
                self.next_restart_at = now + 8
                self.changed()


# ---------------------------------------------------------------- GitHub (mock)
GHS = {"connected": False, "login": None, "name": None, "avatar_url": None, "method": None,
       "client_id": None, "poll_interval_seconds": 120, "rate_limit_remaining": None,
       "device": None}


def rand_sha():
    return secrets.token_hex(20)


LANGS = ["Python", "Python", "Python", "JavaScript", None, "TypeScript", "Go"]
REPOS = []
HEADS = {}  # full_name -> {branch: {sha, message, date}}


def make_repos():
    names = ["ticket-bot", "utility-bot", "music-bot", "bots-monorepo", "welcome-bot", "modlogger",
             "dotfiles", "website", "advent-of-code", "deleted-repo"]
    names += [f"experiment-{i:02d}" for i in range(1, 36)]
    now = time.time()
    for i, n in enumerate(names):
        full = f"example-user/{n}"
        REPOS.append({"full_name": full, "name": n, "owner": "example-user", "private": i % 3 == 0,
                      "default_branch": "master" if n == "website" else "main",
                      "description": {"ticket-bot": "Ticket system with transcripts",
                                      "bots-monorepo": "All my small bots in one place (one folder each)",
                                      "music-bot": "", "dotfiles": "My config files <script>alert(1)</script>"}.get(n, f"Repository {n}"),
                      "language": LANGS[i % len(LANGS)], "pushed_at": iso_ts(now - i * 5400 - 300),
                      "html_url": f"https://github.com/{full}"})
        HEADS[full] = {b: {"sha": rand_sha(), "message": f"Update {b} handlers", "date": iso_ts(now - i * 5400 - 300)}
                       for b in (["main", "dev", "feature/slash-commands"] if n != "website" else ["master"])}


def repo_files(full, subdir):
    name = full.split("/")[1]
    if name == "bots-monorepo":
        files = {"ticket/main.py": b"print('ticket')\n", "ticket/requirements.txt": b"discord.py\n",
                 "music/bot.py": b"print('music')\n", "README.md": b"# monorepo\n"}
    else:
        files = {"main.py": f"# {name}\nprint('hello from {name}')\n".encode(), "requirements.txt": b"discord.py>=2.3\n",
                 "cogs/general.py": b"# cog\n", "data/state.json": b"{}\n", ".env": b"NOT=imported\n", "README.md": b"# readme\n"}
    if subdir:
        sd = subdir.strip("/") + "/"
        files = {k[len(sd):]: v for k, v in files.items() if k.startswith(sd)}
        if not files:
            raise ApiErr(400, f"Folder {subdir} not found in the repository")
    files.pop(".env", None)
    return files


def gh_json():
    return {k: GHS[k] for k in ("connected", "login", "name", "avatar_url", "method", "client_id",
                                 "poll_interval_seconds", "rate_limit_remaining")}


def gh_connect(method):
    GHS.update(connected=True, login="example-user", name="Example User", method=method, rate_limit_remaining=4987,
               avatar_url=("https://avatars.invalid.example/u/1?v=4" if ARGS.avatar_offline
                           else "https://avatars.githubusercontent.com/u/9919?v=4"), device=None)
    for b in BOTS.values():
        if b.source.get("last_error") == "GitHub is not connected":
            b.source["last_error"] = None
            b.changed()


def need_gh():
    if not GHS["connected"]:
        raise ApiErr(409, "GitHub is not connected")


def gh_source(repo, branch, subdir, auto_update, keep_paths=None):
    return {"type": "github", "repo": repo, "branch": branch, "subdir": subdir or "", "auto_update": bool(auto_update),
            "keep_paths": [str(k) for k in (keep_paths or []) if str(k).strip()],
            "commit_sha": None, "commit_message": None, "commit_date": None, "latest_sha": None,
            "update_available": False, "last_checked_at": None, "last_synced_at": None, "syncing": False,
            "last_error": None}


def gh_check(bot):
    src = bot.source
    src["last_checked_at"] = iso()
    if not GHS["connected"]:
        src["last_error"] = "GitHub is not connected"
        return
    if src["repo"].endswith("/deleted-repo") or src["repo"] not in HEADS:
        if src.get("last_error") != "Repository not found or no access":
            bot.log("GitHub: Repository not found or no access", "system")
        src["last_error"] = "Repository not found or no access"
        return
    head = HEADS[src["repo"]].get(src["branch"])
    if not head:
        src["last_error"] = f"Branch {src['branch']} not found"
        return
    src["last_error"] = None
    src["latest_sha"] = head["sha"]
    src["update_available"] = src["commit_sha"] != head["sha"]


def gh_sync(bot):
    gh_check(bot)
    if bot.source.get("last_error"):
        bot.changed()
        return
    bot.source["syncing"] = True
    bot.log(f"Pulling {bot.source['repo']}@{bot.source['branch']}…", "system")
    bot.sync_until = time.time() + 3
    bot.changed()


def gh_finish_sync(bot):
    src = bot.source
    head = HEADS[src["repo"]][src["branch"]]
    files = repo_files(src["repo"], src["subdir"])
    keep = src.get("keep_paths") or []
    for k, v in files.items():
        if k in bot.files and any(fnmatch.fnmatch(k, pat) or (pat.endswith("/**") and k.startswith(pat[:-2])) for pat in keep):
            bot.log(f"kept {k} (matches “Never overwrite”)", "system")
            continue
        if k.startswith("data/") and k in bot.files and src.get("commit_sha"):
            continue  # unchanged upstream -> local runtime data kept
        if k in bot.files and bot.files[k] != v:
            bot.files[f"backups/{time.strftime('%Y%m%d-%H%M%S')}/{k}"] = bot.files[k]
            bot.log(f"overwrote locally modified {k} (old copy in backups/)", "system")
        bot.files[k] = v
    src.update(syncing=False, commit_sha=head["sha"], commit_message=head["message"], commit_date=head["date"],
               latest_sha=head["sha"], update_available=False, last_synced_at=iso(), last_error=None)
    bot.log(f"Updated to {head['sha'][:7]} — {head['message']}", "system")
    detect_entrypoint(bot)
    if bot.state in ("running", "starting"):
        bot.log("Restarting to apply the update", "system")
        bot._begin()
    elif bot.state == "crashed" and not bot.next_restart_at and bot.entrypoint:
        bot.start()
    bot.changed()


def gh_push(full):
    br = next((r["default_branch"] for r in REPOS if r["full_name"] == full), "main")
    HEADS[full][br] = {"sha": rand_sha(), "message": random.choice(["Fix ticket transcript encoding", "Add /stats command",
                       "Bump discord.py", "Refactor cogs"]), "date": iso()}


def seed():
    main_py = (b'import discord\nimport os\n\nclient = discord.Client(intents=discord.Intents.default())\n\n'
               b'@client.event\nasync def on_ready():\n    print(f"Logged in as {client.user}")\n\n'
               b'client.run(os.environ["DISCORD_TOKEN"])\n')
    files = {"main.py": main_py, "requirements.txt": b"discord.py>=2.3\naiohttp\n",
             "cogs/utility.py": b"from discord.ext import commands\n\n\nclass Utility(commands.Cog):\n\tpass\n",
             "cogs/__init__.py": b"", "data/guilds.json": b'{"approved": [1, 2, 3]}\n',
             "core/checks.py": b"def is_admin(ctx):\n    return ctx.author.guild_permissions.administrator\n",
             "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
             "README.md": b"# Utility Bot\n" + b"Some notes.\n" * 80}
    a = Bot("Utility Bot", "Moderation and utility commands", files=files,
            env={"DISCORD_TOKEN": "MTIzNDU2Nzg5MDEyMzQ1Njc4.GmX9aB.s3cr3t-t0k3n-value-3FYU",
                 "GUILD_ID": "118095", "API_KEY": "abc"})
    a.state, a.pid, a.started_at = "running", 4242, time.time() - 3 * 3600 - 17 * 60
    a.needs_restart = True
    for i in range(40):
        a.log(random.choice(SAMPLE_LINES).format(n=i), "stderr" if i % 9 == 0 else "stdout")
    big = {f"data/cache/user_{i:05d}.json": b'{"x": 1}' for i in range(3000)}
    big.update({"bot.py": b"print('ticket bot')\n", "tickets/views.py": b"# views\n"})
    b = Bot("Ticket Bot", "Support tickets with transcripts", entrypoint="bot.py", files=big)
    b.exit_code = 0
    c = Bot("Music Bot", "", files={"main.py": b"print('music')\n", "requirements.txt": b"wavelink\n"})
    c.state, c.exit_code, c.restart_count, c.next_restart_at = "crashed", 1, 2, time.time() + 9
    c.flaky_after = 20
    c.log("KeyError: 'guild_id'", "stderr")
    c.log("Process exited with code 1", "system")
    d = Bot("Welcome Bot", "Greets new members", entrypoint="welcome.py",
            files={"welcome.py": b"print('hi')\n", "requirements.txt": b"discord.py\n"})
    d._begin(install=True)
    e = Bot("Mod Logger", "Audit log mirror", files={"main.py": b"print(1)\n", "requirements.txt": b"discord.py\n"})
    e.state, e.last_error, e.exit_code = "crashed", "Dependency install failed (exit 1)", 1
    f = Bot("Scratch", "No entrypoint yet", entrypoint=None, files={"a.py": b"", "b.py": b""})
    for bot in (a, b, c, d, e, f):
        BOTS[bot.id] = bot
    a.source = gh_source("example-user/utility-bot", "main", "", False)
    head = HEADS["example-user/utility-bot"]["main"]
    a.source.update(commit_sha=rand_sha(), commit_message="Add slash command cooldowns",
                    commit_date=iso_ts(time.time() - 86400 * 2), latest_sha=head["sha"], update_available=True,
                    last_checked_at=iso_ts(time.time() - 40), last_synced_at=iso_ts(time.time() - 86400 * 2))
    b.source = gh_source("example-user/ticket-bot", "main", "", True)
    tb = HEADS["example-user/ticket-bot"]["main"]
    b.source.update(commit_sha=tb["sha"], commit_message=tb["message"], commit_date=tb["date"], latest_sha=tb["sha"],
                    last_checked_at=iso_ts(time.time() - 70), last_synced_at=iso_ts(time.time() - 3600))
    e.source = gh_source("example-user/deleted-repo", "main", "", True)
    e.source.update(commit_sha=rand_sha(), commit_message="Initial commit", commit_date=iso_ts(time.time() - 86400 * 30),
                    last_checked_at=iso_ts(time.time() - 100), last_error="Repository not found or no access")


# ---------------------------------------------------------------- uploads
def is_skipped(path: str) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return name in SKIP_FILES or name.endswith((".pyc", ".pyo"))


def clean_path(p: str) -> str:
    p = p.replace("\\", "/").lstrip("/")
    pp = PurePosixPath(p)
    if not p or any(part in ("..", "") for part in pp.parts) or ":" in pp.parts[0]:
        raise ApiErr(400, f"Invalid path: {p}")
    return str(pp)


def parse_multipart(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 300 * 1024 * 1024:
        raise ApiErr(413, "Upload too large (max 300 MB)")
    chunks, read = [], 0
    while read < length:
        chunk = handler.rfile.read(min(65536, length - read))
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
        if ARGS.throttle:
            time.sleep(len(chunk) / (ARGS.throttle * 1024))
    body = b"".join(chunks)
    raw = b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + body
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append((str(filename), data))
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def parse_dotenv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if KEY_RE.match(k):
            out[k] = v
    return out


def normalize_upload(files):
    """Returns (files dict, env dict) per SPEC upload rules."""
    if not files:
        raise ApiErr(400, "No files uploaded")
    if len(files) == 1 and files[0][0].lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(files[0][1]))
        except zipfile.BadZipFile:
            raise ApiErr(400, "Not a valid zip file")
        files = [(i.filename, zf.read(i)) for i in zf.infolist() if not i.is_dir()]
    elif len(files) == 1 and files[0][0].lower().endswith(".py"):
        return {files[0][0].split("/")[-1]: files[0][1]}, {}
    out = {}
    for name, data in files:
        p = clean_path(name)
        if not is_skipped(p):
            out[p] = data
    if len(out) > 20000:
        raise ApiErr(413, "Too many files (max 20 000)")
    tops = {p.split("/")[0] for p in out}
    if len(tops) == 1 and all("/" in p for p in out):
        out = {p.split("/", 1)[1]: d for p, d in out.items()}
    env = {}
    if ".env" in out:
        env = parse_dotenv(out.pop(".env").decode("utf-8", "replace"))
    return out, env


def top_py(bot: Bot) -> list[str]:
    tops = sorted(p for p in bot.files if "/" not in p and p.endswith(".py"))
    for pref in reversed(ENTRY_PREF):
        if pref in tops:
            tops.remove(pref)
            tops.insert(0, pref)
    return tops


def detect_entrypoint(bot: Bot):
    if bot.entrypoint and bot.entrypoint in bot.files:
        return
    py = [p for p in bot.files if p.endswith(".py")]
    if len(py) == 1:
        bot.entrypoint = py[0]
        return
    for c in ENTRY_PREF:
        if c in bot.files:
            bot.entrypoint = c
            return
    tops = [p for p in py if "/" not in p]
    bot.entrypoint = tops[0] if len(tops) == 1 else None


def entry(bot: Bot, path: str) -> dict:
    if path in bot.files:
        return {"path": path, "name": path.split("/")[-1], "type": "file",
                "size": len(bot.files[path]), "modified_at": bot.updated_at}
    if path in bot.all_dirs():
        return {"path": path, "name": path.split("/")[-1], "type": "dir", "size": 0, "modified_at": bot.updated_at}
    raise ApiErr(404, "File not found")


def masked(v: str) -> str:
    return "••••••••" + v[-4:] if len(v) >= 12 else "•" * 8


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "BotDockMock/0.1"

    def log_message(self, fmt, *args):
        if not ARGS.quiet:
            super().log_message(fmt, *args)

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")

    def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_out(self, data, status=200, extra=None):
        self.send(status, json.dumps(data).encode(), "application/json", extra)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiErr(400, "Invalid JSON body")

    def session(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "botdock_session" and v in STATE["sessions"]:
                return v
        return None

    def new_session(self) -> dict:
        tok = secrets.token_hex(16)
        STATE["sessions"].add(tok)
        return {"Set-Cookie": f"botdock_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"}

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        if not path.startswith("/api/") and not path.startswith("/__mock/"):
            return self.static(path)
        try:
            if ARGS.allowed_hosts and (self.headers.get("Host") or "").split(":")[0] not in ARGS.allowed_hosts.split(","):
                raise ApiErr(403, f"Host {self.headers.get('Host')!r} is not allowed. Add it to BOTDOCK_ALLOWED_HOSTS.")
            if method != "GET" and self.headers.get("X-BotDock") != "1":
                raise ApiErr(403, "Missing X-BotDock header")
            if ARGS.latency and not path.endswith("/stream") and path != "/api/events":
                time.sleep(ARGS.latency / 1000)
            self.api(method, path, qs)
        except ApiErr as e:
            self.json_out({"detail": e.detail}, e.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        root = STATIC.resolve()
        f = (root / rel).resolve()
        if not str(f).startswith(str(root)) or not f.is_file():
            f = root / "index.html"
        self.send(200, f.read_bytes(), MIME.get(f.suffix.lower(), "application/octet-stream"))

    def api(self, m, path, qs):
        if path == "/api/health":
            return self.json_out({"status": "ok", "version": "0.1.2"})
        if path == "/api/auth/state":
            out = {"setup_required": STATE["password"] is None, "authenticated": self.session() is not None}
            if STATE["password"] is None and STATE["setup_code"]:
                out["setup_code_required"] = True
            return self.json_out(out)
        if path == "/api/auth/setup" and m == "POST":
            if STATE["password"] is not None:
                raise ApiErr(409, "Already set up")
            body = self.body_json()
            pw = body.get("password") or ""
            if STATE["setup_code"] and (body.get("setup_code") or "").strip().upper() != STATE["setup_code"]:
                raise ApiErr(403, "Wrong setup code")
            if len(pw) < 8:
                raise ApiErr(400, "Password must be at least 4 characters")
            STATE["password"] = pw
            return self.json_out({"ok": True}, extra=self.new_session())
        if path == "/api/auth/login" and m == "POST":
            now = time.time()
            STATE["fails"] = [t for t in STATE["fails"] if now - t < 300]
            if len(STATE["fails"]) >= 5:
                raise ApiErr(429, "Too many failed attempts. Try again in a few minutes.")
            if self.body_json().get("password") != STATE["password"]:
                STATE["fails"].append(now)
                raise ApiErr(401, "Wrong password")
            return self.json_out({"ok": True}, extra=self.new_session())
        if path == "/__mock/expire":
            STATE["sessions"].clear()
            return self.json_out({"ok": True})

        sess = self.session()
        if not sess:
            raise ApiErr(401, "Not authenticated")
        if path == "/api/auth/logout" and m == "POST":
            STATE["sessions"].discard(sess)
            return self.json_out({"ok": True}, extra={"Set-Cookie": "botdock_session=; Max-Age=0; Path=/"})
        if path == "/api/auth/password" and m == "POST":
            b = self.body_json()
            if b.get("current_password") != STATE["password"]:
                raise ApiErr(400, "Current password is wrong")
            if len(b.get("new_password") or "") < 8:
                raise ApiErr(400, "New password must be at least 4 characters")
            STATE["password"] = b["new_password"]
            STATE["sessions"] = {sess}
            return self.json_out({"ok": True})
        if path == "/api/system":
            with LOCK:
                return self.json_out({
                    "version": "0.1.2", "python_version": "3.12.7", "platform": "Linux-6.6.44-x86_64 (mock)",
                    "data_dir": "/data", "bot_count": len(BOTS),
                    "running_count": sum(b.state == "running" for b in BOTS.values()),
                    "uptime_seconds": int(time.time() - STARTED + 86400 * 3),
                    "disk": {"total_bytes": 4 * 1024**4, "free_bytes": int(2.7 * 1024**4)}})
        if path.startswith("/api/github") or path == "/api/bots/github":
            with LOCK:
                return self.github_api(m, path, qs)
        if path == "/__mock/push" and m == "POST":
            with LOCK:
                gh_push((qs.get("repo") or ["example-user/ticket-bot"])[0])
            return self.json_out({"ok": True})
        if path == "/api/events":
            return self.stream(EVENTS.add(), [], EVENTS)
        if path == "/api/bots":
            if m == "GET":
                with LOCK:
                    bots = sorted(BOTS.values(), key=lambda b: b.name.lower())
                    return self.json_out({"bots": [b.json() for b in bots]})
            if m == "POST":
                fields, files = parse_multipart(self)
                name = (fields.get("name") or "").strip()
                if not 1 <= len(name) <= 60:
                    raise ApiErr(400, "Name must be 1-60 characters")
                with LOCK:
                    if any(b.name.lower() == name.lower() for b in BOTS.values()):
                        raise ApiErr(409, f"A bot named “{name}” already exists")
                    out, env = normalize_upload(files)
                    bot = Bot(name, fields.get("description") or "", entrypoint=(fields.get("entrypoint") or None),
                              files=out, env=env)
                    detect_entrypoint(bot)
                    BOTS[bot.id] = bot
                    bot.changed()
                    return self.json_out({"bot": bot.json(), "imported_env_keys": list(env),
                                          "detected_entrypoints": top_py(bot)}, 201)

        mm = re.match(r"^/api/bots/(b_[0-9a-f]+)(/.*)?$", path)
        if not mm:
            raise ApiErr(404, "Not found")
        with LOCK:
            bot = BOTS.get(mm.group(1))
        if not bot:
            raise ApiErr(404, "Bot not found")
        return self.bot_api(m, bot, mm.group(2) or "", qs)

    def github_api(self, m, path, qs):
        if path == "/api/github" and m == "GET":
            return self.json_out(gh_json())
        if path == "/api/github" and m == "DELETE":
            GHS.update(connected=False, login=None, name=None, avatar_url=None, method=None, rate_limit_remaining=None)
            return self.json_out({"ok": True})
        if path == "/api/github/settings" and m == "PUT":
            b = self.body_json()
            if "poll_interval_seconds" in b:
                v = b["poll_interval_seconds"]
                if not isinstance(v, int) or not 30 <= v <= 3600:
                    raise ApiErr(400, "Poll interval must be between 30 and 3600 seconds")
                GHS["poll_interval_seconds"] = v
            if "client_id" in b:
                GHS["client_id"] = (b["client_id"] or "").strip() or None
            return self.json_out(gh_json())
        if path == "/api/github/token" and m == "POST":
            tok = (self.body_json().get("token") or "").strip()
            if not tok or tok == "bad":
                raise ApiErr(400, "GitHub rejected this token (401 Bad credentials)")
            gh_connect("token")
            return self.json_out(gh_json())
        if path == "/api/github/device/start" and m == "POST":
            if not GHS["client_id"]:
                raise ApiErr(409, "Set the OAuth App client ID first")
            code = "-".join("".join(random.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(4)) for _ in range(2))
            GHS["device"] = {"polls": 0, "expires": time.time() + 900}
            return self.json_out({"user_code": code, "verification_uri": "https://github.com/login/device",
                                  "expires_in": 900, "interval": 5})
        if path == "/api/github/device/poll" and m == "POST":
            d = GHS["device"]
            if not d:
                return self.json_out({"status": "connected" if GHS["connected"] else "none"})
            d["polls"] += 1
            if d["polls"] < 4:
                return self.json_out({"status": "pending"})
            if GHS["client_id"] == "expire":
                GHS["device"] = None
                return self.json_out({"status": "expired"})
            if GHS["client_id"] == "deny":
                GHS["device"] = None
                return self.json_out({"status": "denied"})
            gh_connect("device")
            return self.json_out({"status": "connected"})
        if path == "/api/github/repos" and m == "GET":
            need_gh()
            q = ((qs.get("q") or [""])[0]).lower()
            page = max(1, int((qs.get("page") or ["1"])[0]))
            hits = [r for r in REPOS if q in r["name"].lower()]
            return self.json_out({"repos": hits[(page - 1) * 30: page * 30], "has_more": len(hits) > page * 30})
        mb = re.match(r"^/api/github/repos/([^/]+)/([^/]+)/branches$", path)
        if mb and m == "GET":
            need_gh()
            full = f"{unquote(mb.group(1))}/{unquote(mb.group(2))}"
            if full not in HEADS or full.endswith("deleted-repo"):
                raise ApiErr(404, "Repository not found or no access")
            r = next(x for x in REPOS if x["full_name"] == full)
            return self.json_out({"branches": list(HEADS[full]), "default_branch": r["default_branch"]})
        if path == "/api/bots/github" and m == "POST":
            need_gh()
            b = self.body_json()
            name = (b.get("name") or "").strip()
            if not 1 <= len(name) <= 60:
                raise ApiErr(400, "Name must be 1-60 characters")
            if any(x.name.lower() == name.lower() for x in BOTS.values()):
                raise ApiErr(409, f"A bot named “{name}” already exists")
            repo = b.get("repo") or ""
            if repo not in HEADS or repo.endswith("deleted-repo"):
                raise ApiErr(400, "Repository not found or no access")
            branch = b.get("branch") or next(r["default_branch"] for r in REPOS if r["full_name"] == repo)
            if branch not in HEADS[repo]:
                raise ApiErr(400, f"Branch {branch} not found")
            files = repo_files(repo, b.get("subdir") or "")
            LOCK.release()
            try:
                time.sleep(1.2)  # "downloading"
            finally:
                LOCK.acquire()
            ep = b.get("entrypoint") or None
            if ep and ep not in files:
                raise ApiErr(400, f"Entrypoint {ep} does not exist")
            bot = Bot(name, b.get("description") or "", entrypoint=ep, files=files)
            detect_entrypoint(bot)
            bot.source = gh_source(repo, branch, b.get("subdir") or "", b.get("auto_update", True), b.get("keep_paths"))
            head = HEADS[repo][branch]
            bot.source.update(commit_sha=head["sha"], commit_message=head["message"], commit_date=head["date"],
                              latest_sha=head["sha"], last_checked_at=iso(), last_synced_at=iso())
            BOTS[bot.id] = bot
            bot.log(f"Downloaded {repo}@{branch} ({head['sha'][:7]})", "system")
            bot.changed()
            return self.json_out({"bot": bot.json(), "imported_env_keys": [], "detected_entrypoints": top_py(bot)}, 201)
        raise ApiErr(404, "Not found")

    def bot_api(self, m, bot: Bot, sub: str, qs):
        def q1(k, d=None):
            return (qs.get(k) or [d])[0]

        if sub == "" and m == "GET":
            return self.json_out({"bot": bot.json()})
        if sub == "/source" and m == "PUT":
            b = self.body_json()
            with LOCK:
                if b.get("type") == "upload":
                    bot.source = {"type": "upload"}
                elif b.get("type") == "github":
                    need_gh()
                    repo = b.get("repo") or ""
                    if repo not in HEADS:
                        raise ApiErr(400, "Repository not found or no access")
                    branch = b.get("branch") or "main"
                    if branch not in HEADS[repo]:
                        raise ApiErr(400, f"Branch {branch} not found")
                    old = bot.source if bot.source.get("type") == "github" else None
                    new = gh_source(repo, branch, b.get("subdir") or "", b.get("auto_update", True), b.get("keep_paths"))
                    if old and old["repo"] == repo:
                        for k in ("commit_sha", "commit_message", "commit_date", "last_synced_at"):
                            new[k] = old[k]
                    bot.source = new
                    gh_check(bot)
                else:
                    raise ApiErr(400, "type must be github or upload")
                bot.changed()
                return self.json_out({"bot": bot.json()})
        if sub in ("/source/check", "/source/sync") and m == "POST":
            with LOCK:
                if bot.source.get("type") != "github":
                    raise ApiErr(409, "This bot is not linked to GitHub")
                need_gh()
                if sub.endswith("check"):
                    gh_check(bot)
                    bot.changed()
                else:
                    if bot.source.get("syncing"):
                        raise ApiErr(409, "A sync is already running")
                    gh_sync(bot)
                return self.json_out({"bot": bot.json()})
        if sub == "" and m == "PATCH":
            b = self.body_json()
            with LOCK:
                if "name" in b:
                    n = str(b["name"]).strip()
                    if not 1 <= len(n) <= 60:
                        raise ApiErr(400, "Name must be 1-60 characters")
                    if any(o is not bot and o.name.lower() == n.lower() for o in BOTS.values()):
                        raise ApiErr(409, "Another bot already has that name")
                    b["name"] = n
                if "restart_policy" in b and b["restart_policy"] not in ("always", "on-failure", "never"):
                    raise ApiErr(400, "Invalid restart policy")
                for k in ("name", "description", "entrypoint", "args", "extra_packages", "restart_policy", "autostart"):
                    if k in b:
                        setattr(bot, k, (b[k] or None) if k == "entrypoint" else b[k])
                bot.mark_dirty()
                return self.json_out({"bot": bot.json()})
        if sub == "" and m == "DELETE":
            with LOCK:
                BOTS.pop(bot.id, None)
                EVENTS.send(sse("bot_deleted", {"id": bot.id}))
            return self.json_out({"ok": True})
        if sub in ("/start", "/stop", "/restart", "/reinstall") and m == "POST":
            with LOCK:
                if sub == "/start":
                    bot.start()
                elif sub == "/stop":
                    bot.stop()
                elif sub == "/restart":
                    if not bot.entrypoint:
                        raise ApiErr(409, "No entrypoint set")
                    bot.log("Restarting…", "system")
                    bot.restart_count, bot.next_restart_at, bot.last_error = 0, None, None
                    bot._begin()
                else:
                    was_running = bot.state in ("running", "starting", "installing")
                    bot.log("Removing virtualenv…", "system")
                    if was_running:
                        bot._begin(install=True)
                    else:
                        bot.log("Dependencies will be installed on next start", "system")
                return self.json_out({"bot": bot.json()})
        if sub == "/code" and m == "POST":
            fields, files = parse_multipart(self)
            with LOCK:
                out, env = normalize_upload(files)
                if fields.get("mode") == "replace":
                    bot.files.clear()
                    bot.dirs.clear()
                bot.files.update(out)
                imported = [k for k in env if k not in bot.env]
                skipped_env = [k for k in env if k in bot.env]
                for k in imported:
                    bot.env[k] = (env[k], iso())
                detect_entrypoint(bot)
                bot.mark_dirty()
                return self.json_out({"bot": bot.json(), "imported_env_keys": imported, "skipped_env_keys": skipped_env,
                                      "detected_entrypoints": top_py(bot)})
        if sub == "/export" and m == "GET":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for p, d in bot.files.items():
                    zf.writestr(p, d)
            return self.send(200, buf.getvalue(), "application/zip",
                             {"Content-Disposition": f'attachment; filename="{bot.name}.zip"'})
        # ---- env
        if sub == "/env" and m == "GET":
            return self.json_out({"vars": [{"key": k, "masked": masked(v), "length": len(v), "updated_at": t}
                                           for k, (v, t) in sorted(bot.env.items())]})
        if sub == "/env/import" and m == "POST":
            b = self.body_json()
            parsed = parse_dotenv(b.get("text") or "")
            imported, skipped = [], []
            with LOCK:
                for k, v in parsed.items():
                    if k in bot.env and not b.get("overwrite"):
                        skipped.append(k)
                    else:
                        bot.env[k] = (v, iso())
                        imported.append(k)
                bot.mark_dirty()
            return self.json_out({"imported": imported, "skipped": skipped})
        me = re.match(r"^/env/(.+)$", sub)
        if me:
            key = unquote(me.group(1))
            if not KEY_RE.match(key) or len(key) > 128:
                raise ApiErr(400, "Invalid key")
            with LOCK:
                if m == "GET":
                    if key not in bot.env:
                        raise ApiErr(404, "No such variable")
                    return self.json_out({"key": key, "value": bot.env[key][0]})
                if m == "PUT":
                    v = self.body_json().get("value")
                    if not isinstance(v, str) or len(v.encode()) > 32768:
                        raise ApiErr(400, "Value must be a string up to 32 KB")
                    bot.env[key] = (v, iso())
                    bot.mark_dirty()
                    return self.json_out({"ok": True})
                if m == "DELETE":
                    if key not in bot.env:
                        raise ApiErr(404, "No such variable")
                    del bot.env[key]
                    bot.mark_dirty()
                    return self.json_out({"ok": True})
        # ---- logs
        if sub == "/logs" and m == "GET":
            tail = min(int(q1("tail", "500")), 5000)
            with LOCK:
                return self.json_out({"lines": bot.logs[-tail:] if tail else []})
        if sub == "/logs" and m == "DELETE":
            with LOCK:
                bot.logs.clear()
            return self.json_out({"ok": True})
        if sub == "/logs/download":
            with LOCK:
                txt = "".join(f"{x['ts']} [{x['stream']}] {x['text']}\n" for x in bot.logs)
            return self.send(200, txt.encode(), "text/plain; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{bot.id}.log"'})
        if sub == "/logs/stream":
            after = int(q1("after", "0") or 0)
            with LOCK:
                q = bot.log_hub.add()
                initial = [sse(None, x) for x in bot.logs if x["seq"] > after]
            return self.stream(q, initial, bot.log_hub)
        # ---- files
        if sub == "/files" and m == "GET":
            with LOCK:
                dirs = sorted(bot.all_dirs(), key=str.lower)
                fl = sorted(bot.files, key=str.lower)
                ents = [entry(bot, d) for d in dirs] + [entry(bot, f) for f in fl]
            return self.json_out({"entries": ents[:5000], "truncated": len(ents) > 5000})
        if sub == "/files/content" and m == "GET":
            p = clean_path(q1("path", ""))
            if p not in bot.files:
                raise ApiErr(404, "File not found")
            data = bot.files[p]
            if b"\x00" not in data and len(data) <= 2 * 1024 * 1024:
                try:
                    return self.json_out({"path": p, "content": data.decode("utf-8"), "size": len(data), "binary": False})
                except UnicodeDecodeError:
                    pass
            return self.json_out({"path": p, "content": None, "size": len(data), "binary": True})
        if sub == "/files/content" and m == "PUT":
            b = self.body_json()
            p = clean_path(b.get("path") or "")
            with LOCK:
                bot.files[p] = (b.get("content") or "").encode()
                bot.mark_dirty()
                return self.json_out({"entry": entry(bot, p)})
        if sub == "/files/download":
            p = clean_path(q1("path", ""))
            if p not in bot.files:
                raise ApiErr(404, "File not found")
            return self.send(200, bot.files[p], "application/octet-stream",
                             {"Content-Disposition": f'attachment; filename="{p.split("/")[-1]}"'})
        if sub == "/files/mkdir" and m == "POST":
            p = clean_path(self.body_json().get("path") or "")
            with LOCK:
                if p in bot.files or p in bot.all_dirs():
                    raise ApiErr(409, "Already exists")
                bot.dirs.add(p)
                bot.changed()
                return self.json_out({"entry": entry(bot, p)})
        if sub == "/files/move" and m == "POST":
            b = self.body_json()
            src, dst = clean_path(b.get("from") or ""), clean_path(b.get("to") or "")
            with LOCK:
                if dst in bot.files or dst in bot.all_dirs():
                    raise ApiErr(409, "Target already exists")
                if src in bot.files:
                    bot.files[dst] = bot.files.pop(src)
                elif src in bot.all_dirs():
                    for f in [f for f in bot.files if f.startswith(src + "/")]:
                        bot.files[dst + f[len(src):]] = bot.files.pop(f)
                    bot.dirs = {dst + d[len(src):] if d == src or d.startswith(src + "/") else d for d in bot.dirs}
                    bot.dirs.add(dst)
                else:
                    raise ApiErr(404, "File not found")
                bot.mark_dirty()
                return self.json_out({"entry": entry(bot, dst)})
        if sub == "/files/delete" and m == "POST":
            p = clean_path(self.body_json().get("path") or "")
            with LOCK:
                if p in bot.files:
                    del bot.files[p]
                elif p in bot.all_dirs():
                    for f in [f for f in bot.files if f.startswith(p + "/")]:
                        del bot.files[f]
                    bot.dirs = {d for d in bot.dirs if d != p and not d.startswith(p + "/")}
                else:
                    raise ApiErr(404, "File not found")
                bot.mark_dirty()
                return self.json_out({"ok": True})
        if sub == "/files/upload" and m == "POST":
            fields, files = parse_multipart(self)
            d = (fields.get("dir") or "").strip("/")
            with LOCK:
                paths = []
                for name, data in files:
                    p = clean_path((d + "/" if d else "") + name)
                    bot.files[p] = data
                    paths.append(p)
                bot.mark_dirty()
                return self.json_out({"entries": [entry(bot, p) for p in paths]})
        raise ApiErr(404, "Not found")

    def stream(self, q: queue.Queue, initial: list[str], hub: Hub):
        try:
            self.send_response(200)
            self.common_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n" + "".join(initial).encode())
            self.wfile.flush()
            while True:
                try:
                    batch = [q.get(timeout=15)]
                    while len(batch) < 500:
                        try:
                            batch.append(q.get_nowait())
                        except queue.Empty:
                            break
                    out = "".join(batch)
                except queue.Empty:
                    out = ": heartbeat\n\n"
                self.wfile.write(out.encode())
                self.wfile.flush()
                if self.session() is None:  # session revoked -> end the stream
                    break
        except OSError:
            pass
        finally:
            hub.remove(q)


def ticker():
    n = 0
    while True:
        time.sleep(1)
        n += 1
        now = time.time()
        with LOCK:
            for i, bot in enumerate(list(BOTS.values())):
                bot.tick(now, i == 0)
            if n % 15 == 0:  # mock poller: every 15 s regardless of the interval setting
                for bot in BOTS.values():
                    if bot.source.get("type") == "github" and not bot.source.get("syncing"):
                        before = (bot.source.get("latest_sha"), bot.source.get("last_error"))
                        gh_check(bot)
                        # auto-update never does the FIRST sync of a linked bot; only an explicit Pull does
                        if bot.source.get("auto_update") and bot.source.get("update_available") and bot.source.get("commit_sha"):
                            gh_sync(bot)
                        elif (bot.source.get("latest_sha"), bot.source.get("last_error")) != before:
                            bot.changed()
            if n % 90 == 0:
                gh_push("example-user/ticket-bot")
            if n % 5 == 0:
                for bot in BOTS.values():
                    if bot.state == "running":
                        EVENTS.send(sse("bot", bot.json()))


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--setup", action="store_true", help="start in first-run setup state")
    ap.add_argument("--empty", action="store_true", help="no seeded bots")
    ap.add_argument("--chatty", type=int, default=0, help="lines/s for the first bot when running")
    ap.add_argument("--throttle", type=int, default=0, help="upload read speed in KB/s")
    ap.add_argument("--latency", type=int, default=120, help="artificial API latency in ms")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--github", action="store_true", help="start with GitHub connected")
    ap.add_argument("--allowed-hosts", default="", help="comma-separated hostnames; others get 403")
    ap.add_argument("--avatar-offline", action="store_true", help="avatar URL that never loads")
    ARGS = ap.parse_args()
    if ARGS.setup:
        STATE["password"] = None
        STATE["setup_code"] = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        print(f"First-run setup code: {STATE['setup_code']}", flush=True)
    make_repos()
    if ARGS.github:
        GHS["client_id"] = "Iv1.mockclientid"
        gh_connect("device")
    if not ARGS.empty:
        seed()
    threading.Thread(target=ticker, daemon=True).start()
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    srv.daemon_threads = True
    print(f"BotDock MOCK on http://{ARGS.host}:{ARGS.port}  (password: {STATE['password'] or '<setup>'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
