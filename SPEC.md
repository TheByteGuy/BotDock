# BotDock — spec

One Docker container that hosts many Python bots (Discord bots mostly), managed from a web UI.
Target: HexOS / TrueNAS SCALE custom app (linux/amd64). Developed on Windows, so the backend must
also run natively on Windows for local testing (`python -m botdock`).

This file is the contract between the backend and the frontend. If you must deviate, edit this file
in the same change and say so in your final report.

## Stack

- Python 3.12, FastAPI + uvicorn, stdlib `sqlite3`, `cryptography` (Fernet), `python-multipart`, `psutil`.
- Frontend: static SPA, **no build step, no CDN** (the NAS may be offline). Vanilla JS ES modules + CSS
  in `botdock/static/`, served by FastAPI at `/`. Any third-party JS must be vendored into `static/vendor/`.
- Tests: `pytest` + `httpx` (FastAPI TestClient) in `tests/`.

## Layout

```
BotDock/
  botdock/
    __main__.py      # python -m botdock  -> uvicorn on BOTDOCK_HOST:BOTDOCK_PORT
    app.py           # FastAPI app factory create_app(data_dir=None), lifespan starts/stops supervisor
    config.py        # env settings
    db.py            # sqlite schema + helpers
    auth.py          # password, sessions, CSRF header check
    crypto.py        # Fernet key management, encrypt/decrypt
    bots.py          # bot CRUD, upload/extract, entrypoint detection, .env import
    supervisor.py    # process lifecycle, venv/deps install, restart policy, logs, stats
    files.py         # safe file browser operations
    api/             # routers
    static/          # frontend (frontend agent owns this directory exclusively)
  tests/
  requirements.txt
```

## Settings (env)

| Var | Default | Meaning |
|---|---|---|
| `BOTDOCK_DATA` | `./data` (`/data` in the image) | persistent dir |
| `BOTDOCK_HOST` / `BOTDOCK_PORT` | `0.0.0.0` / `8080` | listen address |
| `BOTDOCK_PASSWORD` | unset | if set and no admin password exists yet, seeds it (skips first-run setup) |
| `BOTDOCK_SECRET_KEY` | unset | Fernet key; otherwise generated at `$DATA/secret.key` (mode 0600) |
| `BOTDOCK_COOKIE_SECURE` | `0` | set `Secure` on the session cookie |
| `BOTDOCK_USE_VENV` | `1` | `0` = dev/test mode: bots run on BotDock's own interpreter, no venv, no pip install |

Data dir:

```
$DATA/botdock.db
$DATA/secret.key
$DATA/bots/<bot_id>/src/     # bot code; the bot's cwd; bots write their own data files here
$DATA/bots/<bot_id>/venv/    # per-bot virtualenv
$DATA/bots/<bot_id>/logs/bot.log (+ .1 .2 .3, rotate at 5 MB)
```

## Security rules

- Single admin account (password only). scrypt hash in DB. Sessions: random 32-byte token in an
  `HttpOnly; SameSite=Strict; Path=/` cookie named `botdock_session`, stored hashed in DB, 30-day expiry.
- Every `/api/*` route requires a session except `GET /api/health`, `GET /api/auth/state`,
  `POST /api/auth/setup`, `POST /api/auth/login`.
- Every non-GET `/api/*` request must carry header `X-BotDock: 1` (CSRF guard) → otherwise 403.
- Login rate limit: 5 failures / 5 min per client IP → 429.
- Env var values are encrypted at rest (Fernet). Never log them. Never return them in list responses.
- All file paths are resolved and must stay inside the bot's `src/` (reject `..`, absolute paths,
  symlink escapes). Zip extraction must be zip-slip safe.
- Bot processes get a clean environment: `PATH` (venv bin first), `HOME`, `LANG`, `TZ`,
  `PYTHONUNBUFFERED=1`, `VIRTUAL_ENV`, plus on Windows the vars Python needs (`SYSTEMROOT`, `TEMP`, ...),
  plus the bot's own env vars. No `BOTDOCK_*` variable ever reaches a bot.
- Responses send `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, and a CSP of `default-src 'self'; img-src 'self' data: https://avatars.githubusercontent.com; style-src 'self' 'unsafe-inline'`.

## Errors

Every error is JSON `{"detail": "human readable message"}` with a fitting status
(400 validation, 401 no session, 403 CSRF, 404, 409 conflict/invalid state, 413 too large, 429).

## Objects

### Bot

```json
{
  "id": "b_3f9a1c2e",              // "b_" + 8 hex
  "name": "Utility Bot",           // 1-60 chars, unique (case-insensitive)
  "description": "",
  "entrypoint": "main.py",         // relative posix path inside src, or null if undetermined
  "args": "",                      // extra CLI args (shlex-split)
  "extra_packages": "",            // newline/space separated pip requirement specifiers, installed in addition to requirements.txt
  "restart_policy": "on-failure",  // "always" | "on-failure" | "never"
  "autostart": true,               // start when BotDock boots
  "created_at": "2026-11-15T20:00:00Z",
  "updated_at": "2026-11-15T20:00:00Z",
  "env_count": 3,
  "file_count": 42,
  "size_bytes": 123456,
  "has_requirements": true,        // src/requirements.txt exists
  "needs_restart": false,          // code/env/settings changed since the running process started
  "runtime": {
    "state": "running",            // "stopped" | "installing" | "starting" | "running" | "stopping" | "crashed"
    "pid": 1234,                   // null unless a process exists
    "started_at": "2026-11-15T20:01:00Z",   // null unless running
    "exit_code": null,             // last exit code, null if never exited
    "restart_count": 0,            // automatic restarts since last manual start
    "next_restart_at": null,       // ISO time when a backoff restart is scheduled
    "cpu_percent": 0.4,            // null unless running; process + children
    "memory_bytes": 48234496,      // null unless running; RSS of process + children
    "last_error": null             // short string, e.g. "Dependency install failed (exit 1)" / "No entrypoint set"
  }
}
```

All timestamps are UTC ISO-8601 with a `Z` suffix.

### LogLine

```json
{"seq": 1012, "ts": "2026-11-15T20:01:02.123Z", "stream": "stdout", "text": "Logged in as ..."}
```

`stream` is `stdout` | `stderr` | `system` (BotDock's own messages: "Installing dependencies…",
"Process exited with code 1", "Restarting in 4s"). `seq` increases monotonically per bot for the life
of the BotDock process. pip output is logged as `system`.

### FileEntry

```json
{"path": "cogs/utility.py", "name": "utility.py", "type": "file", "size": 5120, "modified_at": "…Z"}
```

`type` is `file` | `dir`. Paths are posix, relative to `src/`, never start with `/`.

## API

### Auth

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/health` | | `{"status":"ok","version":"0.1.0"}` |
| GET | `/api/auth/state` | | `{"setup_required": bool, "authenticated": bool}` |
| POST | `/api/auth/setup` | `{"password"}` (min 4 chars) | 200 `{"ok":true}` + session cookie; 409 if already set up |
| POST | `/api/auth/login` | `{"password"}` | 200 `{"ok":true}` + cookie; 401 bad password; 429 limited |
| POST | `/api/auth/logout` | | `{"ok":true}`, clears cookie |
| POST | `/api/auth/password` | `{"current_password","new_password"}` | `{"ok":true}`; all other sessions are revoked; wrong current password → **400** (not 401, so the UI doesn't bounce to login) |

`POST /api/auth/login` before setup → 409 `Setup required`. The rate limit is checked before the
password, so the 6th attempt within 5 min gets 429 even with the right password.

### System

| GET | `/api/system` | | `{"version","python_version","platform","data_dir","bot_count","running_count","uptime_seconds","disk":{"total_bytes","free_bytes"}}` |

### Bots

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/bots` | | `{"bots":[Bot]}` sorted by name |
| POST | `/api/bots` | multipart, see **Upload** + fields `name` (required), `description`, `entrypoint` (optional override) | 201 `{"bot":Bot,"imported_env_keys":[...],"detected_entrypoints":[...]}` |
| GET | `/api/bots/{id}` | | `{"bot":Bot}` |
| PATCH | `/api/bots/{id}` | JSON, any of `name, description, entrypoint, args, extra_packages, restart_policy, autostart` | `{"bot":Bot}` |
| DELETE | `/api/bots/{id}` | | `{"ok":true}`; stops the bot, removes its directory |
| POST | `/api/bots/{id}/code` | multipart, see **Upload** + field `mode` = `merge` (default) or `replace` | `{"bot":Bot,"imported_env_keys":[...],"detected_entrypoints":[...]}` |
| POST | `/api/bots/{id}/start` | | `{"bot":Bot}` (202-style: returns immediately, state `installing`/`starting`) |
| POST | `/api/bots/{id}/stop` | | `{"bot":Bot}` |
| POST | `/api/bots/{id}/restart` | | `{"bot":Bot}` |
| POST | `/api/bots/{id}/reinstall` | | `{"bot":Bot}`; wipes the venv, reinstalls deps, restarts the bot if it was running |
| GET | `/api/bots/{id}/export` | | `application/zip` of `src/` (attachment `<name>.zip`) |
| GET | `/api/events` | | SSE stream of bot changes, see **Events** |

**Upload** (multipart/form-data): repeated `files` parts; each part's filename is the file's
**relative path** using `/` (e.g. `MyBot/cogs/tickets.py`) — the frontend sets it via
`formData.append("files", file, relativePath)`. Rules:

- One file ending in `.py` → single-file bot; stored as `src/<filename>`.
- One file ending in `.zip` → extracted.
- Otherwise → a folder upload; files stored at their relative paths.
- If every path shares one top-level directory, strip it.
- Skip: `__pycache__/`, `.git/`, `.venv/`, `venv/`, `env/`, `node_modules/`, `.idea/`, `.vscode/`, `*.pyc`, `*.pyo`, `.DS_Store`, `Thumbs.db`.
- Limits: 300 MB total, 20 000 files → 413.
- A top-level `.env` is **not** stored: its `KEY=VALUE` pairs are imported as the bot's env vars
  (existing keys are not overwritten) and the key names are returned in `imported_env_keys`.
- `mode=merge` overwrites uploaded paths and keeps everything else (bots keep their `data/*.json`).
  `mode=replace` deletes `src/` contents first.
- Entrypoint detection (when not given and the current one is null or no longer exists): the single
  `.py` file; else first existing of `main.py`, `bot.py`, `app.py`, `run.py`, `start.py`, `__main__.py`;
  else the only top-level `.py`; else null. `detected_entrypoints` lists all top-level `.py` files,
  best candidate first.
- Uploading code to a running bot sets `needs_restart`; it does not restart it.

**Lifecycle** (supervisor):

- `start`: if no entrypoint → 409. Ensure venv exists (`python -m venv`), then install deps when the
  fingerprint (sha256 of requirements.txt + extra_packages + python version) differs from the last
  successful install: `pip install -r requirements.txt` and `pip install <extra_packages>`, output
  streamed to the bot log as `system` lines; state `installing`. Failure → state `crashed`,
  `last_error` set, no auto-restart. Then spawn `<venv python> -u <entrypoint> <args>` with
  `cwd=src/`; state `starting` → `running` once the process has survived 3 s.
- Exit handling: `always` restarts on any exit; `on-failure` on non-zero exit; `never` never.
  Backoff 2, 4, 8, … max 60 s; counter resets after 60 s of uptime; 10 consecutive rapid failures →
  `crashed` with `last_error`. While waiting, state is `crashed` with `next_restart_at` set.
- `stop`: SIGTERM to the whole process group, SIGKILL after 10 s. On Windows: terminate the process tree
  (psutil). Manual stop never triggers a restart.
- Desired state is persisted: a bot the user started is started again when BotDock boots if
  `autostart` is true; a bot the user stopped stays stopped.
- BotDock shutdown stops every bot gracefully (in parallel) without changing desired state.

### Env vars / secrets

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/bots/{id}/env` | | `{"vars":[{"key":"DISCORD_TOKEN","masked":"••••••••3FYU","length":72,"updated_at":"…Z"}]}` (masked = bullets + last 4 chars, or all bullets if length < 12) |
| GET | `/api/bots/{id}/env/{key}` | | `{"key","value"}` — the reveal call |
| PUT | `/api/bots/{id}/env/{key}` | `{"value"}` | `{"ok":true}` (create or update) |
| DELETE | `/api/bots/{id}/env/{key}` | | `{"ok":true}` |
| POST | `/api/bots/{id}/env/import` | `{"text":"A=1\nB=\"two\"\n# comment","overwrite":true}` | `{"imported":["A","B"],"skipped":[]}` — dotenv syntax incl. `export A=1`, quotes |

Keys must match `^[A-Za-z_][A-Za-z0-9_]*$`, max 128 chars; values max 32 KB. Any change sets `needs_restart` on a running bot.

### Logs

| Method | Path | Result |
|---|---|---|
| GET | `/api/bots/{id}/logs?tail=500` | `{"lines":[LogLine]}` (max 5000; from the in-memory ring buffer, falling back to the log file after a BotDock restart) |
| GET | `/api/bots/{id}/logs/stream?after=<seq>` | SSE; each event `data: <LogLine json>`; first replays buffered lines with `seq > after`; comment heartbeat every 15 s |
| GET | `/api/bots/{id}/logs/download` | `text/plain` attachment of the full current log (all rotations, oldest first) |
| DELETE | `/api/bots/{id}/logs` | `{"ok":true}`; clears buffer and files |

SSE endpoints authenticate by the session cookie (EventSource sends it same-origin).

### Events

`GET /api/events` — SSE. Events: `event: bot` + `data: Bot` whenever a bot's runtime state or
settings change, `event: bot_deleted` + `data: {"id"}`; additionally a `bot` event for every running
bot every 5 s (stats refresh). Heartbeat comment every 15 s.

### Files

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/bots/{id}/files` | | `{"entries":[FileEntry],"truncated":false}` — full recursive listing in depth-first tree order (inside each folder: dirs first, then files, case-insensitive); capped at 5000 entries (`"truncated": true`) |
| GET | `/api/bots/{id}/files/content?path=` | | `{"path","content","size","binary":false}`; binary or > 2 MB → `{"binary":true,"content":null}` |
| PUT | `/api/bots/{id}/files/content` | `{"path","content"}` | `{"entry":FileEntry}`; creates parent dirs |
| GET | `/api/bots/{id}/files/download?path=` | | raw file attachment |
| POST | `/api/bots/{id}/files/mkdir` | `{"path"}` | `{"entry":FileEntry}` |
| POST | `/api/bots/{id}/files/move` | `{"from","to"}` | `{"entry":FileEntry}`; 409 if target exists |
| POST | `/api/bots/{id}/files/delete` | `{"path"}` | `{"ok":true}` (recursive for dirs) |
| POST | `/api/bots/{id}/files/upload` | multipart: `files` parts (filename = relative path), field `dir` (target dir, `""` = root) | `{"entries":[FileEntry]}` — no stripping, no .env import |

Writes/moves/deletes/uploads set `needs_restart` on a running bot.

## Frontend

Static files in `botdock/static/` (`index.html`, `css/`, `js/`). Hash routing. `GET /` and any
non-`/api` path that is not a static file returns `index.html`.

Screens:

1. **Setup / Login** — driven by `/api/auth/state`.
2. **Dashboard** `#/` — bot cards/rows: name, state pill, uptime, CPU, memory, quick
   start/stop/restart, "needs restart" hint; live via `/api/events`. Empty state with a big
   "Add your first bot" drop zone. System footer (version, disk free).
3. **New bot** `#/new` — drag & drop or pick: a `.py` file, a folder (`webkitdirectory` +
   `DataTransferItem.webkitGetAsEntry()` traversal for drops), or a `.zip`. Shows the file list
   summary (count, size, whether requirements.txt / .env were found), name (prefilled from the
   file/folder name), entrypoint (auto, editable after upload if ambiguous), upload progress (XHR).
   After create: go to the bot page; if `.env` keys were imported say which.
4. **Bot** `#/bots/<id>/<tab>` — header with state, start/stop/restart, needs-restart banner. Tabs:
   - **Console** — live log (SSE), colour by stream, autoscroll with "jump to latest", filter box,
     wrap toggle, download, clear. Handles 5000+ lines without jank (cap DOM lines).
   - **Secrets** — env var table: key, masked value, reveal (eye, fetches value), copy, edit, delete,
     add row, "Paste .env" bulk import dialog. Never keeps revealed values after navigating away.
   - **Files** — tree on the left, text editor on the right (monospace textarea with line numbers,
     Tab inserts spaces, Ctrl+S saves, unsaved-changes guard), new file/folder, rename, delete,
     upload here, download; binary files show a download prompt.
   - **Settings** — name, description, entrypoint (dropdown of `.py` files + free text), args,
     extra pip packages, restart policy, autostart; "Update code" (upload with merge/replace
     choice), "Reinstall dependencies", "Export zip", danger zone delete (type the name to confirm).
5. **Account** `#/account` — change password, log out, system info.

All mutating requests send `X-BotDock: 1`. A 401 anywhere returns to the login screen.
Dark theme by default with a light theme toggle (remember in localStorage). Works on a phone.
Toast notifications for results/errors. No frameworks required; keep it small and readable.

## Backend implementation notes (clarifications made while implementing)

These refine the contract above; the frontend can rely on them.

- **Layout**: besides the modules listed, the backend has `paths.py` (path validation), `uploads.py`
  (streaming multipart + upload planning), `envfile.py` (dotenv parser), `logstore.py`, `procutil.py`
  (process spawning/killing), `events.py`, `util.py`. Uploads use BotDock's own streaming multipart
  parser (files go straight to `$DATA/tmp`), so Starlette's 1000 files/fields limits do not apply.
- **Upload paths**: a path is rejected with **400** (whole upload refused, nothing written) if it is
  absolute, has a drive letter, contains `..`, a backslash, `:` or control/`<>"|?*` characters, a
  component ending in `.`/space, or a Windows reserved name (`CON`, `NUL`, `COM1`, `LPT1`, ...).
  Zip entries that are symlinks or encrypted → 400; an invalid zip → 400. Zip limits (300 MB
  uncompressed, 20 000 files) → 413. Non-multipart request bodies over 16 MB → 413.
  `__MACOSX/` is skipped in addition to the listed skip rules. Skip rules apply to directory names at
  any depth. Merge uploads that would turn an existing folder into a file (or vice versa) → 409.
- **Entrypoint override** (`entrypoint` form field on create *and* on `/code`, or PATCH) must name an
  existing file in `src/` → otherwise 400. PATCH `entrypoint: null` or `""` clears it.
- **needs_restart** is set while a process is installing/starting/running by: code uploads, file
  writes/moves/deletes/uploads, env changes, and PATCH of `entrypoint`, `args` or `extra_packages`
  (name/description/restart_policy/autostart don't need a restart). Cleared when a new process spawns.
- **Lifecycle details**: `start` on a bot that is already installing/starting/running is a no-op
  (200). `start` during a pending backoff restart starts immediately. Manual start/restart and boot
  autostart reset `restart_count`. `stop` always ends in state `stopped` (also from `crashed`);
  `exit_code`/`last_error` are kept. `pid` is set while a bot process exists (starting/running/stopping).
  A process that exits without being restarted ends in `stopped` (exit 0) or `crashed` (non-zero,
  `last_error` = "Process exited with code N"). A crash after ≥ 60 s uptime restarts after 2 s and
  does not count towards the 10 rapid failures. `start`/`restart` with a missing entrypoint file → 409.
- **Bot process environment**: also gets `PYTHONIOENCODING=utf-8`. `HOME` is BotDock's `HOME`
  (Windows: `USERPROFILE`), or `$DATA/home` if unset. Env keys starting with `BOTDOCK_` are rejected
  (400 on PUT, listed in `skipped` on import). pip runs with the clean environment but **without** the
  bot's secrets. `env/import` `overwrite` defaults to `false`.
- **Logs**: on boot each bot's ring buffer is pre-filled from its log files (last 5000 lines, `seq`
  renumbered from 1), so `/logs` and `/logs/stream` serve old output after a BotDock restart. Log SSE
  events carry `id: <seq>`; `after` defaults to `Last-Event-ID` or 0. Every SSE stream starts with
  `retry: 3000`. Log file lines are `<ts> [<stream>] <text>`.
- **Files**: `mkdir` on an existing path → 409. Deleting the root (`path: ""`) → 400. Editor writes
  over 10 MB → 413. Binary detection: NUL byte in the first 8 KB or not valid UTF-8.
- **Frontend missing**: if `botdock/static/index.html` doesn't exist, non-API GETs return 503
  `text/plain` "BotDock frontend not installed". Static files are served with `Cache-Control: no-cache`
  and explicit MIME types (`.js` → `text/javascript`).

---

# GitHub integration (added after v1 of this spec)

Goal: sign in to GitHub, pick one of your repos (private ones too) as a bot's code source, and
optionally have BotDock notice new commits, pull them and restart the bot. The NAS is on a LAN with
no public address, so everything is **outbound polling** — no webhooks, no OAuth redirect.

## Sign-in

Two methods, one account at a time, token stored Fernet-encrypted in the DB:

1. **Device flow** (the "Sign in with GitHub" button). Needs the client ID of a GitHub OAuth App
   with *Device Flow* enabled (no client secret, no callback URL). The client ID is a BotDock setting
   (also seedable with env `BOTDOCK_GITHUB_CLIENT_ID`). Scope requested: `repo`.
2. **Personal access token** paste (classic with `repo`, or fine-grained with Contents: read).
   Zero setup; always available.

## Objects

Bot gains a `source` field:

```json
"source": {"type": "upload"}
```
or
```json
"source": {
  "type": "github",
  "repo": "example-user/ticket-bot",
  "branch": "main",
  "subdir": "",                    // optional path inside the repo that is the bot root
  "auto_update": true,
  "commit_sha": "a1b2c3d…",        // full sha currently deployed, null before first sync
  "commit_message": "first line",
  "commit_date": "…Z",
  "latest_sha": "…",               // newest sha seen on the branch
  "update_available": false,       // latest_sha != commit_sha
  "last_checked_at": "…Z",
  "last_synced_at": "…Z",
  "syncing": false,
  "last_error": null               // e.g. "Repository not found or no access", "GitHub is not connected"
}
```

## API

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/github` | | `{"connected":bool,"login":str\|null,"name":str\|null,"avatar_url":str\|null,"method":"device"\|"token"\|null,"client_id":str\|null,"poll_interval_seconds":120,"rate_limit_remaining":int\|null}` |
| PUT | `/api/github/settings` | any of `{"client_id","poll_interval_seconds"}` (interval 30–3600) | same shape as GET |
| POST | `/api/github/token` | `{"token"}` | GET shape; 400 if GitHub rejects the token |
| POST | `/api/github/device/start` | | `{"user_code":"WDJB-MJHT","verification_uri":"https://github.com/login/device","expires_in":900,"interval":5}`; 409 if no client ID is set. The device_code stays server-side. |
| POST | `/api/github/device/poll` | | `{"status":"pending"\|"connected"\|"expired"\|"denied"\|"none"}` — the server itself respects GitHub's `interval`/`slow_down`; the UI may call this every 2 s |
| DELETE | `/api/github` | | `{"ok":true}`; forgets the token. Linked bots stay linked and report `last_error`. |
| GET | `/api/github/repos?q=&page=1` | | `{"repos":[{"full_name","name","owner","private":bool,"default_branch","description","language","pushed_at","html_url"}],"has_more":bool}` — repos the account can access, most recently pushed first, 30 per page; `q` filters by name (case-insensitive substring) |
| GET | `/api/github/repos/{owner}/{repo}/branches` | | `{"branches":["main","dev"],"default_branch":"main"}` |
| POST | `/api/bots/github` | `{"name","description"?,"repo","branch"?,"subdir"?,"auto_update"?:true,"entrypoint"?}` | 201, same shape as `POST /api/bots` — downloads the repo and creates the bot |
| PUT | `/api/bots/{id}/source` | `{"type":"github","repo","branch","subdir","auto_update"}` or `{"type":"upload"}` | `{"bot":Bot}` — link an existing bot to a repo / change branch / toggle auto-update / unlink. Linking does not pull by itself. |
| POST | `/api/bots/{id}/source/check` | | `{"bot":Bot}` — ask GitHub for the newest commit now |
| POST | `/api/bots/{id}/source/sync` | | `{"bot":Bot}` — pull now (returns immediately, `syncing: true`; progress in the bot log as `system` lines and via `bot` events) |

All of these need a session; 409 `{"detail":"GitHub is not connected"}` when a call needs a token and there is none.
CSP `img-src` additionally allows `https://avatars.githubusercontent.com` (UI must fall back to initials when it can't load).

## Sync rules

- Code is fetched as the branch tarball through the API (no `git` binary needed), `subdir` applied,
  same skip list and limits as uploads. A top-level `.env` in a repo is ignored (never imported).
- BotDock remembers a manifest `{path: sha256}` of what the last sync wrote. On the next sync, per file:
  - new upstream, or upstream content changed since the last sync → write it (upstream wins; if the
    local file had also been changed, log a `system` line "overwrote locally modified <path>").
  - upstream unchanged since the last sync → leave the local file alone. This is what keeps a bot's
    runtime-rewritten `data/*.json` intact even when those files are committed in the repo.
  - removed upstream → delete locally only if the local file is unmodified since the last sync.
  - files BotDock never synced (created by the bot or the user) are never touched.
- After a sync: entrypoint re-detected only if the current one no longer exists; dependency
  fingerprint is re-evaluated on next start as usual.
- A sync on a **running** bot restarts it. A bot in `crashed` state (gave up or failed install) is
  started. A bot the user stopped is updated and stays stopped.
- Uploading code or editing files on a GitHub-linked bot is still allowed (local changes follow the rules above).

## Poller

One background task. Every `poll_interval_seconds` it checks each GitHub-linked bot's branch head with
a conditional request (`ETag` / `If-None-Match`, which does not consume rate limit on 304), updates
`latest_sha`, `last_checked_at`, `update_available`, and for bots with `auto_update` runs a sync when
the head changed. Errors go to `source.last_error` (and one `system` log line when the error changes),
never crash the poller. Backs off when rate-limited (honours `X-RateLimit-Reset` / `Retry-After`).

## Frontend

- **Account** page: "GitHub" card — connected state (avatar, login, method, Disconnect), or the two
  sign-in methods: "Sign in with GitHub" (device flow: shows the code large with a copy button and an
  "Open github.com/login/device" link that opens in a new tab, polls until connected/expired; if no
  client ID is set, shows a short how-to for creating the OAuth App and a field to save the client ID)
  and "Use a personal access token". Poll interval setting.
- **New bot**: a second source choice next to upload — "From GitHub": searchable repo list
  (private badge, language, last push), branch picker, optional subfolder, auto-update toggle, name
  prefilled from the repo name. If GitHub isn't connected, the choice leads to the Account card.
- **Dashboard** rows/cards: a small GitHub mark + `repo@branch` for linked bots; "update available" hint.
- **Bot → Settings**: "Source" section — for GitHub bots: repo link, branch, subdir, deployed commit
  (short sha + message + relative time), last checked, auto-update toggle, "Check now", "Pull now",
  "Unlink"; `last_error` shown inline. For upload bots: "Link to a GitHub repo".

## GitHub — backend implementation notes

- **Settings**: `BOTDOCK_GITHUB_API_URL` (default `https://api.github.com`) and `BOTDOCK_GITHUB_URL` (device-flow
  host; derived from the API URL when unset: `api.github.com` → `github.com`, `https://host/api/v3` → `https://host`).
  This is also the GitHub Enterprise hook. `BOTDOCK_GITHUB_CLIENT_ID` only seeds the stored client ID when none is set.
- **Errors**: interactive GitHub calls map to `409` not connected / token rejected (`"GitHub rejected the token (sign
  in again)"`), `404` repo/branch not found, `429` rate limited, `502` GitHub unreachable or other upstream failure.
  `POST /api/github/token` with a rejected token → `400`. `device/start` with an unknown client ID → `502`.
  `PUT /api/github/settings` with an invalid interval → `400`; `client_id: ""`/`null` clears it.
- **device/poll** returns `pending` without contacting GitHub until GitHub's `interval` has passed (slow_down
  raises it). After `connected`/`expired`/`denied` the pending flow is forgotten, so the next poll returns `none`.
- **Repos**: BotDock fetches up to 1000 repos (cached for 60 s), then filters `q` against name and full_name and pages
  locally (30 per page). Branches list the default branch first.
- **PUT source**: `branch` may be omitted or `""` to use the repo's default branch (that needs a connection). Linking
  without a connection is allowed when a branch is given. Changing repo/branch/subdir resets `latest_sha`, the ETag
  and `last_error`, but keeps the manifest. `{"type":"upload"}` unlinks and forgets the manifest. After linking,
  BotDock runs a check in the background so `latest_sha`/`update_available` fill in shortly.
- **check** on a bot that is not linked → 409. GitHub errors during check/sync are not HTTP errors: they land
  in `source.last_error` and the response is `{"bot"}` as usual.
- **sync** downloads the tarball of the head commit it just resolved (so the files match `commit_sha`). It runs under
  the same per-bot lock as start/stop/upload/delete. The tarball is unpacked into `$DATA/tmp` and every path is
  checked before anything in `src/` is written, so a failed or hostile download leaves `src/` untouched.
  Symlinks, hardlinks and device entries in the tarball are skipped, with a `system` log line. A hostile path
  (`..`, absolute) or any name rejected by the upload path rules fails the whole sync. Size/count limits are the
  upload limits (300 MB / 20 000 files). An upstream file that collides with a local folder fails the sync before
  anything is written. Folders left empty by an upstream delete are removed.
- **Sync rule detail**: "upstream unchanged" means the upstream content hash equals the manifest entry. In that case
  a locally *deleted* file also stays deleted. The `overwrote locally modified <path>` line is also logged when a
  new upstream file replaces a file the bot/user created that has different content.
- **Poller**: the first check runs 10 s after boot, then every `poll_interval_seconds`. After a sync fails for a
  given head, the poller does not retry that head; it retries when a new commit appears (or on a manual "Pull now").
  A linked bot that has never been synced (`commit_sha: null`) is pulled by the poller if `auto_update` is on.
  An error is logged to the bot as one `system` line `GitHub: <error>` each time it changes.
- **Restart on sync** keeps the desired state: a running bot is restarted, a `crashed` bot (including one waiting for
  a backoff restart) is started, and a stopped bot stays stopped.

## Backend fixes after the frontend run (supersede the notes above where they differ)

- **Path rules by platform**: everywhere, BotDock rejects `..`, absolute paths, `C:` / `C:/…` drive prefixes,
  backslashes and control characters. The Windows-only rules apply only when BotDock runs on Windows: reserved names
  (`CON`, `aux.py`, `COM1`, …), `<>:"|?*`, and components ending in a dot or space. So on the Linux NAS a repo or
  upload containing `aux.py` or `a:b.txt` is accepted.
- **SSE and sessions**: `/api/events` and `/api/bots/{id}/logs/stream` end as soon as their session is logged out
  or revoked by a password change. Every stream also re-checks its session every 2 s, which catches expiry and
  revocation from anywhere else. A revoked stream does not deliver lines that were still queued.
  The UI's EventSource will then reconnect and get 401.
- **Bytecode**: bot processes run with `PYTHONDONTWRITEBYTECODE=1` (a bot env var of the same name overrides it), so
  `src/` does not fill with `__pycache__`. `GET /api/bots/{id}/export` applies the upload skip list (`__pycache__/`,
  `*.pyc`, `.git/`, `venv/`, …).
- **Linking while disconnected**: `PUT /api/bots/{id}/source` still succeeds without a GitHub connection, and
  `source.last_error` is immediately `"GitHub is not connected"`. Connecting clears it.

## Security & sync fixes after review (supersede earlier text where they differ)

### Settings (new env)
| Var | Default | Meaning |
|---|---|---|
| `BOTDOCK_ALLOWED_HOSTS` | empty | extra `Host` names to accept (comma separated, ports ignored); `*` disables the Host check |
| `BOTDOCK_PASSWORD_FILE` | unset | file holding the seed admin password; read once at startup (before any bot starts), then deleted. Takes precedence over `BOTDOCK_PASSWORD` |

### Host / Origin (DNS rebinding)
- Every request (API and static) must carry a `Host` that is an IP literal, `localhost`, a single-label name
  (`nas`), a name ending in `.local` `.lan` `.home` `.internal` `.home.arpa` (or `.localhost`), or a name listed in
  `BOTDOCK_ALLOWED_HOSTS`. Otherwise → 403 `{"detail": "Host '…' is not allowed. … add it to BOTDOCK_ALLOWED_HOSTS …"}`.
- Non-GET `/api` requests that carry an `Origin` header must have Origin host:port == Host (default ports ignored),
  else 403. `Origin: null` is refused.
- Non-GET error responses carry `Connection: close`, so the server doesn't read the rest of a rejected body.

### First-run setup code
- When no admin password exists at startup, BotDock creates a one-time setup code (`XXXX-XXXX-XXXX`), prints it
  to stdout (the container log) and writes it to `$DATA/setup-code.txt` (mode 0600). A new code is generated at
  every start until setup is done.
- `GET /api/auth/state` → `{"setup_required", "authenticated", "setup_code_required"}`; `setup_code_required` is
  true exactly when setup is required and a code exists.
- `POST /api/auth/setup` body `{"password", "setup_code"}`. Case, spaces and dashes in the code are ignored.
  A wrong or missing code → 403. Attempts are rate-limited like login (5 failures / 5 min per IP → 429). On success
  the file is deleted.
- Seed passwords (`BOTDOCK_PASSWORD_FILE` / `BOTDOCK_PASSWORD`) shorter than 4 characters or on a small denylist
  (`changeme`, `change-me-please`, `password`, `password123`, `admin`, `botdock`, …) are refused with a warning in
  the log, and the setup-code flow is used instead.

### Request bodies
- Non-multipart `/api` bodies are capped on bytes actually received (chunked bodies included), not only on
  `Content-Length`: 64 KB for `/api/auth/login` and `/api/auth/setup`, 16 MB elsewhere → 413.
- Multipart uploads are capped on received bytes too (300 MB of file data, plus at most 64 MB of multipart
  overhead in total). Upload endpoints check the session before reading the body, so an unauthenticated upload
  gets 401 without anything being buffered or written to disk.

### Shutdown
- During BotDock shutdown each bot gets 5 s after SIGTERM before SIGKILL (manual stop still waits 10 s). uvicorn's
  graceful HTTP drain is 2 s, so BotDock finishes within Docker's default 10 s stop timeout.

### GitHub sync (contract changes)
- **No automatic first sync.** Linking a bot (`PUT /api/bots/{id}/source`) never changes its files, and the
  poller never performs a bot's first sync. Only `POST /api/bots/{id}/source/sync` (or creating the bot with
  `POST /api/bots/github`) establishes `commit_sha`. Until then the poller only checks, so `latest_sha` is set and
  `update_available` is `true` while `commit_sha` is `null`. The UI shows that as "not pulled yet".
- **Backups.** Before a sync overwrites a file that was modified locally (or that BotDock never wrote), the old
  copy is saved to `$DATA/bots/<id>/backups/<UTC yyyymmddThhmmssZ>/<path>`, outside `src/`. One `system` log line
  says where. Only the newest 10 backup sets per bot are kept. Bot deletion removes them too.
  (A sync only ever deletes files that are unmodified since the last sync, so deletes never need a backup.)
- **`keep_paths`** (new `source` field, array of strings, default `[]`): glob patterns, posix, relative to the bot
  root.
  - Syntax: `*` and `?` match within one path segment; `**` matches across segments; `**/x` matches `x` in any
    folder.
  - A pattern without wildcards also covers everything below it: `data` = `data` + `data/**`. A trailing `/`
    means the folder's contents.
  - A file matching `keep_paths` that already exists locally is never written or deleted by a sync. If it does
    not exist locally yet, it is created from the repo.
  - Accepted in the `PUT /api/bots/{id}/source` body (omit it to leave it unchanged) and in the
    `POST /api/bots/github` body. Returned in `source.keep_paths`.
  - Limits: at most 50 patterns of 200 chars. No `..`, leading `/`, backslash or control characters → 400.

### Upload responses
- `POST /api/bots` and `POST /api/bots/{id}/code` (and `POST /api/bots/github`, always `[]`) also return
  `skipped_env_keys`: keys from the uploaded `.env` that were not imported. That is either because they already
  existed (left unchanged), or because the key is invalid or reserved (`BOTDOCK_*`, bad name, value > 32 KB).
