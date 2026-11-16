"""Bot CRUD, code uploads, entrypoint detection, env vars and Bot JSON serialisation."""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import files as fileops
from .config import Settings
from .crypto import Crypto, InvalidToken
from .db import Database
from .envfile import parse_dotenv
from .events import EventHub
from .paths import clean_relpath
from .supervisor import Supervisor, rmtree_retry
from .uploads import Plan, StagedUpload, apply_plan, build_plan
from .util import ApiError, iso

BOT_ID_RE = re.compile(r"^b_[0-9a-f]{8}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PREFERRED_ENTRYPOINTS = ("main.py", "bot.py", "app.py", "run.py", "start.py", "__main__.py")
RESTART_POLICIES = ("always", "on-failure", "never")
MAX_ENV_VALUE_BYTES = 32 * 1024
MAX_ENV_KEY_LEN = 128
STATS_TTL = 60.0
BULLETS = "•" * 8


def detect_entrypoints(src: Path) -> list[str]:
    """All top-level .py files, best candidate first."""
    try:
        top = sorted((p.name for p in src.iterdir() if p.is_file() and p.name.lower().endswith(".py")),
                     key=str.lower)
    except OSError:
        return []
    return [n for n in PREFERRED_ENTRYPOINTS if n in top] + [n for n in top if n not in PREFERRED_ENTRYPOINTS]


def choose_entrypoint(single_py: str | None, detected: list[str]) -> str | None:
    if single_py:
        return single_py
    for name in PREFERRED_ENTRYPOINTS:
        if name in detected:
            return name
    if len(detected) == 1:
        return detected[0]
    return None


def mask(value: str) -> str:
    return BULLETS + value[-4:] if len(value) >= 12 else BULLETS


def validate_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "Name is required")
    name = raw.strip()
    if len(name) > 60:
        raise ApiError(400, "Name must be at most 60 characters")
    if any(ord(c) < 32 for c in name):
        raise ApiError(400, "Name contains control characters")
    return name


def validate_env_key(key: str) -> str:
    if not isinstance(key, str) or not ENV_KEY_RE.match(key) or len(key) > MAX_ENV_KEY_LEN:
        raise ApiError(400, "Invalid key: use letters, digits and _ (not starting with a digit), max 128 chars")
    if key.upper().startswith("BOTDOCK_"):
        raise ApiError(400, "Keys starting with BOTDOCK_ are reserved")
    return key


def validate_env_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "value must be a string")
    if len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
        raise ApiError(400, "Value too large (max 32 KB)")
    return value


class BotService:
    def __init__(self, settings: Settings, db: Database, crypto: Crypto, supervisor: Supervisor, events: EventHub):
        self.settings = settings
        self.db = db
        self.crypto = crypto
        self.supervisor = supervisor
        self.events = events
        self._stats: dict[str, tuple[float, int, int]] = {}
        self.github = None  # GitHubService, wired up by the app

    # lookup / serialisation ---------------------------------------------------------
    def get_row(self, bot_id: str) -> dict:
        if not BOT_ID_RE.match(bot_id or ""):
            raise ApiError(404, "Bot not found")
        row = self.db.get_bot(bot_id)
        if row is None:
            raise ApiError(404, "Bot not found")
        return row

    def src(self, bot_id: str) -> Path:
        return self.settings.src_dir(bot_id)

    def invalidate(self, bot_id: str) -> None:
        self._stats.pop(bot_id, None)

    def _file_stats(self, bot_id: str) -> tuple[int, int]:
        cached = self._stats.get(bot_id)
        now = time.monotonic()
        if cached and now - cached[0] < STATS_TTL:
            return cached[1], cached[2]
        count, size = fileops.count_and_size(self.src(bot_id))
        self._stats[bot_id] = (now, count, size)
        return count, size

    def bot_json(self, row: dict) -> dict:
        bot_id = row["id"]
        count, size = self._file_stats(bot_id)
        return {
            "id": bot_id,
            "name": row["name"],
            "description": row["description"] or "",
            "entrypoint": row["entrypoint"],
            "args": row["args"] or "",
            "extra_packages": row["extra_packages"] or "",
            "restart_policy": row["restart_policy"],
            "autostart": bool(row["autostart"]),
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
            "env_count": self.db.env_count(bot_id),
            "file_count": count,
            "size_bytes": size,
            "has_requirements": (self.src(bot_id) / "requirements.txt").is_file(),
            "needs_restart": self.supervisor.needs_restart(bot_id),
            "runtime": self.supervisor.runtime_json(bot_id),
            "source": self.github.source_json(bot_id) if self.github is not None else {"type": "upload"},
        }

    def get_json(self, bot_id: str) -> dict:
        return self.bot_json(self.get_row(bot_id))

    def list_json(self) -> list[dict]:
        return [self.bot_json(r) for r in self.db.list_bots()]

    def publish(self, bot_id: str) -> None:
        row = self.db.get_bot(bot_id)
        if row is not None:
            self.events.publish("bot", self.bot_json(row))

    # create / update code ------------------------------------------------------------
    def new_id(self) -> str:
        while True:
            bot_id = "b_" + secrets.token_hex(4)
            if self.db.get_bot(bot_id) is None and not self.settings.bot_dir(bot_id).exists():
                return bot_id

    def validate_entrypoint(self, bot_id: str, raw: str) -> str:
        rel = clean_relpath(raw)
        if not (self.src(bot_id) / rel).is_file():
            raise ApiError(400, f"Entrypoint {rel} does not exist")
        return rel

    async def _build_plan(self, staged: StagedUpload) -> Plan:
        return await asyncio.to_thread(
            build_plan, staged, max_bytes=self.settings.max_upload_bytes, max_files=self.settings.max_upload_files)

    async def create(self, staged: StagedUpload) -> dict:
        fields = staged.fields
        name = validate_name(fields.get("name"))
        description = fields.get("description") or ""
        if len(description) > 2000:
            raise ApiError(400, "Description must be at most 2000 characters")
        if self.db.bot_name_taken(name):
            raise ApiError(409, f"A bot named {name!r} already exists")
        plan = await self._build_plan(staged)
        bot_id = self.new_id()
        bot_dir = self.settings.bot_dir(bot_id)
        try:
            src = self.src(bot_id)
            await asyncio.to_thread(apply_plan, src, plan, "replace")
            self.settings.logs_dir(bot_id).mkdir(parents=True, exist_ok=True)
            detected = detect_entrypoints(src)
            override = (fields.get("entrypoint") or "").strip()
            entry = self.validate_entrypoint(bot_id, override) if override else choose_entrypoint(plan.single_py, detected)
            now = time.time()
            try:
                self.db.insert_bot({
                    "id": bot_id, "name": name, "description": description, "entrypoint": entry, "args": "",
                    "extra_packages": "", "restart_policy": "on-failure", "autostart": 1,
                    "desired_state": "stopped", "deps_fingerprint": None, "created_at": now, "updated_at": now,
                })
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, f"A bot named {name!r} already exists") from exc
            imported, skipped = (self._import_env_text(bot_id, plan.env_text, overwrite=False)
                                 if plan.env_text else ([], []))
        except BaseException:
            plan.close()
            if self.db.get_bot(bot_id) is not None:
                self.db.delete_bot(bot_id)
            await asyncio.to_thread(rmtree_retry, bot_dir)
            raise
        plan.close()
        self.supervisor.runtime(bot_id)
        self.publish(bot_id)
        return {"bot": self.get_json(bot_id), "imported_env_keys": imported, "skipped_env_keys": skipped,
                "detected_entrypoints": detected}

    def after_code_change(self, bot_id: str) -> None:
        """New code landed (sync): refresh stats, re-detect a vanished entrypoint, flag the change."""
        self.invalidate(bot_id)
        row = self.db.get_bot(bot_id)
        if row is None:
            return
        src = self.src(bot_id)
        entry = row["entrypoint"]
        if not entry or not (src / entry).is_file():
            entry = choose_entrypoint(None, detect_entrypoints(src))
        self.db.update_bot(bot_id, {"entrypoint": entry})
        self.supervisor.mark_changed(bot_id)

    async def upload_code(self, bot_id: str, staged: StagedUpload) -> dict:
        self.get_row(bot_id)
        mode = (staged.fields.get("mode") or "merge").strip().lower()
        if mode not in ("merge", "replace"):
            raise ApiError(400, "mode must be 'merge' or 'replace'")
        override = (staged.fields.get("entrypoint") or "").strip()
        plan = await self._build_plan(staged)

        async def op() -> dict:
            row = self.get_row(bot_id)
            src = self.src(bot_id)
            await asyncio.to_thread(apply_plan, src, plan, mode)
            self.invalidate(bot_id)
            detected = detect_entrypoints(src)
            entry = row["entrypoint"]
            if override:
                entry = self.validate_entrypoint(bot_id, override)
            elif not entry or not (src / entry).is_file():
                entry = choose_entrypoint(plan.single_py, detected)
            self.db.update_bot(bot_id, {"entrypoint": entry})
            imported, skipped = (self._import_env_text(bot_id, plan.env_text, overwrite=False)
                                 if plan.env_text else ([], []))
            self.supervisor.mark_changed(bot_id)
            return {"imported_env_keys": imported, "skipped_env_keys": skipped, "detected_entrypoints": detected}

        try:
            result = await self.supervisor.serialized(bot_id, op)
        finally:
            plan.close()
        self.publish(bot_id)
        return {"bot": self.get_json(bot_id), **result}

    # settings -----------------------------------------------------------------------
    def patch(self, bot_id: str, data: dict) -> dict:
        row = self.get_row(bot_id)
        updates: dict[str, Any] = {}
        if "name" in data:
            name = validate_name(data["name"])
            if self.db.bot_name_taken(name, exclude_id=bot_id):
                raise ApiError(409, f"A bot named {name!r} already exists")
            updates["name"] = name
        if "description" in data:
            desc = data["description"] or ""
            if not isinstance(desc, str) or len(desc) > 2000:
                raise ApiError(400, "Description must be a string of at most 2000 characters")
            updates["description"] = desc
        if "entrypoint" in data:
            ep = data["entrypoint"]
            if ep is None or (isinstance(ep, str) and ep.strip() == ""):
                updates["entrypoint"] = None
            elif isinstance(ep, str):
                updates["entrypoint"] = self.validate_entrypoint(bot_id, ep.strip())
            else:
                raise ApiError(400, "entrypoint must be a string or null")
        if "args" in data:
            args = data["args"] or ""
            if not isinstance(args, str) or len(args) > 4000:
                raise ApiError(400, "args must be a string of at most 4000 characters")
            try:
                shlex.split(args)
            except ValueError as exc:
                raise ApiError(400, f"Invalid args: {exc}") from exc
            updates["args"] = args
        if "extra_packages" in data:
            extra = data["extra_packages"] or ""
            if not isinstance(extra, str) or len(extra) > 4000:
                raise ApiError(400, "extra_packages must be a string of at most 4000 characters")
            updates["extra_packages"] = extra
        if "restart_policy" in data:
            if data["restart_policy"] not in RESTART_POLICIES:
                raise ApiError(400, "restart_policy must be one of: always, on-failure, never")
            updates["restart_policy"] = data["restart_policy"]
        if "autostart" in data:
            if not isinstance(data["autostart"], bool):
                raise ApiError(400, "autostart must be a boolean")
            updates["autostart"] = 1 if data["autostart"] else 0
        if updates:
            try:
                self.db.update_bot(bot_id, updates)
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "A bot with that name already exists") from exc
            if any(k in updates and updates[k] != row[k] for k in ("entrypoint", "args", "extra_packages")):
                self.supervisor.mark_changed(bot_id)
            self.publish(bot_id)
        return self.get_json(bot_id)

    async def delete(self, bot_id: str) -> None:
        self.get_row(bot_id)

        async def op() -> None:
            if self.db.get_bot(bot_id) is None:
                raise ApiError(404, "Bot not found")
            await self.supervisor.stop_locked(bot_id, set_desired=False)
            self.supervisor.forget(bot_id)
            self.db.delete_bot(bot_id)
            self.invalidate(bot_id)
            try:
                await asyncio.to_thread(rmtree_retry, self.settings.bot_dir(bot_id))
            except OSError:
                pass  # leftover files are harmless; the bot is gone from the DB

        await self.supervisor.serialized(bot_id, op)
        self.events.publish("bot_deleted", {"id": bot_id})

    # files ----------------------------------------------------------------------------
    async def file_mutation(self, bot_id: str, fn, *args) -> Any:
        """Run a blocking file operation under the bot lock and flag the change."""
        self.get_row(bot_id)

        async def op() -> Any:
            self.get_row(bot_id)
            return await asyncio.to_thread(fn, self.src(bot_id), *args)

        result = await self.supervisor.serialized(bot_id, op)
        self.invalidate(bot_id)
        self.supervisor.mark_changed(bot_id)
        self.publish(bot_id)
        return result

    async def upload_files(self, bot_id: str, staged: StagedUpload) -> list[dict]:
        self.get_row(bot_id)
        prefix = fileops.clean_dir_param(staged.fields.get("dir", ""))
        plan = await asyncio.to_thread(
            build_plan, staged, max_bytes=self.settings.max_upload_bytes,
            max_files=self.settings.max_upload_files, bot_mode=False, prefix=prefix)

        def apply(src: Path) -> list[dict]:
            written = apply_plan(src, plan, "merge")
            return [fileops.entry_for(rel, Path(src, *rel.split("/"))) for rel in written]

        try:
            return await self.file_mutation(bot_id, apply)
        finally:
            plan.close()

    # env ------------------------------------------------------------------------------
    def list_env(self, bot_id: str) -> list[dict]:
        self.get_row(bot_id)
        out = []
        for row in self.db.env_rows(bot_id):
            try:
                masked = mask(self.crypto.decrypt(row["value_enc"]))
            except InvalidToken:
                masked = BULLETS
            out.append({"key": row["key"], "masked": masked, "length": row["length"], "updated_at": iso(row["updated_at"])})
        return out

    def reveal_env(self, bot_id: str, key: str) -> dict:
        self.get_row(bot_id)
        row = self.db.env_get(bot_id, key)
        if row is None:
            raise ApiError(404, f"No variable named {key}")
        try:
            return {"key": key, "value": self.crypto.decrypt(row["value_enc"])}
        except InvalidToken as exc:
            raise ApiError(409, "Cannot decrypt this value (was the secret key changed?)") from exc

    def _store_env(self, bot_id: str, key: str, value: str) -> None:
        self.db.env_set(bot_id, key, self.crypto.encrypt(value), len(value), time.time())

    def set_env(self, bot_id: str, key: str, value: Any) -> None:
        self.get_row(bot_id)
        validate_env_key(key)
        self._store_env(bot_id, key, validate_env_value(value))
        self._env_changed(bot_id)

    def delete_env(self, bot_id: str, key: str) -> None:
        self.get_row(bot_id)
        if not self.db.env_delete(bot_id, key):
            raise ApiError(404, f"No variable named {key}")
        self._env_changed(bot_id)

    def import_env(self, bot_id: str, text: Any, overwrite: bool) -> dict:
        self.get_row(bot_id)
        if not isinstance(text, str):
            raise ApiError(400, "text must be a string")
        if len(text) > 1024 * 1024:
            raise ApiError(400, "text too large (max 1 MB)")
        imported, skipped = self._import_env_text(bot_id, text, overwrite=overwrite)
        if imported:
            self._env_changed(bot_id)
        return {"imported": imported, "skipped": skipped}

    def _import_env_text(self, bot_id: str, text: str, overwrite: bool) -> tuple[list[str], list[str]]:
        pairs, bad_keys = parse_dotenv(text)
        latest: dict[str, str] = {}
        for key, value in pairs:
            latest[key] = value
        existing = self.db.env_keys(bot_id)
        imported: list[str] = []
        skipped: list[str] = list(dict.fromkeys(bad_keys))
        for key, value in latest.items():
            try:
                validate_env_key(key)
                validate_env_value(value)
            except ApiError:
                skipped.append(key)
                continue
            if key in existing and not overwrite:
                skipped.append(key)
                continue
            self._store_env(bot_id, key, value)
            imported.append(key)
        return imported, skipped

    def _env_changed(self, bot_id: str) -> None:
        self.db.update_bot(bot_id, {})
        self.supervisor.mark_changed(bot_id)
        self.publish(bot_id)
