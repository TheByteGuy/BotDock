"""Bot process lifecycle: venv + dependency install, spawn, restart policy with backoff, stop,
logs (ring buffer + files + live subscribers) and resource stats.

Concurrency model
-----------------
* Everything here runs on the event loop thread, except the reader/waiter threads inside
  ``ProcessHandle`` which only hand data back via ``call_soon_threadsafe``.
* Every user-facing operation on a bot (start/stop/restart/reinstall/delete/code upload/file
  writes) goes through ``serialized()``: a per-bot asyncio.Lock, run in a shielded task so a client
  disconnect can never abandon an operation half way.
* A running bot is driven by one ``_run`` task per bot (install -> spawn -> wait -> backoff ->
  respawn). Operations cancel that task; its ``finally`` kills whatever process it owns, so there
  is never more than one process per bot.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shlex
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import psutil

from .config import Settings
from .crypto import Crypto, InvalidToken
from .db import Database
from .logstore import LogWriter, Subscriber
from .procutil import IS_WINDOWS, ProcessHandle, terminate_tree
from .util import ApiError, iso, now_iso

log = logging.getLogger("botdock.supervisor")

STATES = ("stopped", "installing", "starting", "running", "stopping", "crashed")
WINDOWS_ENV_KEEP = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "NUMBER_OF_PROCESSORS", "COMPUTERNAME", "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "OS",
)


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if IS_WINDOWS else "bin")


def rmtree_retry(path: Path, attempts: int = 10) -> None:
    """shutil.rmtree that tolerates Windows' delayed handle release after a process dies."""

    def onexc(func, p, exc):  # make read-only files deletable, then retry once
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            raise exc from None

    for i in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onexc=onexc)
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(0.2 * (i + 1))


class BotRuntime:
    def __init__(self, bot_id: str, writer: LogWriter, buffer_lines: int):
        self.bot_id = bot_id
        self.state = "stopped"
        self.pid: int | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.restart_count = 0
        self.next_restart_at: float | None = None
        self.last_error: str | None = None
        self.needs_restart = False
        self.rapid_failures = 0
        self.task: asyncio.Task | None = None
        self.mode = "run"  # "run" | "install"
        self.proc: ProcessHandle | None = None
        self.cpu_percent: float | None = None
        self.memory_bytes: int | None = None
        self._ps: dict[int, psutil.Process] = {}
        self.writer = writer
        self.lines: deque[dict] = deque(maxlen=buffer_lines)
        self.seq = 0
        self.subscribers: set[Subscriber] = set()

    # logs ---------------------------------------------------------------------------
    def preload(self, items: list[tuple[str, str, str]]) -> None:
        for ts, stream, text in items:
            self.seq += 1
            self.lines.append({"seq": self.seq, "ts": ts, "stream": stream, "text": text})

    def deliver(self, stream: str, items: list[tuple[str, str]]) -> None:
        """Called on the loop thread with lines already written to the log file."""
        for ts, text in items:
            self.seq += 1
            line = {"seq": self.seq, "ts": ts, "stream": stream, "text": text}
            self.lines.append(line)
            for sub in list(self.subscribers):
                sub.push(line)
                if sub.closed:
                    self.subscribers.discard(sub)

    def system(self, text: str) -> None:
        ts = now_iso(millis=True)
        self.writer.write([(ts, "system", text)])
        self.deliver("system", [(ts, text)])

    def tail(self, n: int) -> list[dict]:
        if n <= 0:
            return []
        return list(self.lines)[-n:]

    def clear_logs(self) -> None:
        self.lines.clear()
        self.writer.clear()

    def task_alive(self) -> bool:
        return self.task is not None and not self.task.done()


class Supervisor:
    def __init__(self, settings: Settings, db: Database, crypto: Crypto,
                 on_change: Callable[[str], None] | None = None):
        self.settings = settings
        self.db = db
        self.crypto = crypto
        self.on_change = on_change or (lambda bot_id: None)
        self.runtimes: dict[str, BotRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.shutting_down = False

    # setup / teardown ---------------------------------------------------------------
    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        for bot in self.db.list_bots():
            self.runtime(bot["id"])
        for bot in self.db.list_bots():
            if bot["desired_state"] == "running" and bot["autostart"]:
                self.runtime(bot["id"]).system("BotDock started; autostarting bot")
                try:
                    await self.start(bot["id"], manual=False)
                except Exception:  # never let one bot break boot
                    log.exception("autostart of %s failed", bot["id"])

    async def shutdown(self) -> None:
        """Stop every bot (in parallel) without touching their desired state."""
        self.shutting_down = True  # also shortens the per-bot SIGTERM grace (see _run's finally)

        async def stop_one(bot_id: str) -> None:
            async def op() -> None:
                rt = self.runtimes.get(bot_id)
                if rt is not None and rt.task_alive():
                    await self._cancel_task(rt, reason="BotDock is shutting down")
                    rt.state = "stopped"
                    rt.next_restart_at = None

            await self.serialized(bot_id, op)

        await asyncio.gather(*(stop_one(b) for b in list(self.runtimes)), return_exceptions=True)
        for rt in self.runtimes.values():
            for sub in list(rt.subscribers):
                sub.close()
            rt.writer.close()

    # helpers ------------------------------------------------------------------------
    def runtime(self, bot_id: str) -> BotRuntime:
        rt = self.runtimes.get(bot_id)
        if rt is None:
            writer = LogWriter(self.settings.logs_dir(bot_id), self.settings.log_max_bytes, self.settings.log_backups)
            rt = BotRuntime(bot_id, writer, self.settings.log_buffer_lines)
            try:
                rt.preload(writer.read_tail(self.settings.log_buffer_lines))
            except OSError:
                pass
            self.runtimes[bot_id] = rt
        return rt

    def forget(self, bot_id: str) -> None:
        rt = self.runtimes.pop(bot_id, None)
        self._locks.pop(bot_id, None)
        if rt is not None:
            for sub in list(rt.subscribers):
                sub.close()
            rt.subscribers.clear()
            rt.writer.close()

    def _lock(self, bot_id: str) -> asyncio.Lock:
        lock = self._locks.get(bot_id)
        if lock is None:
            lock = self._locks[bot_id] = asyncio.Lock()
        return lock

    async def serialized(self, bot_id: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run fn under the bot's lock, in a task that survives cancellation of the caller."""
        lock = self._lock(bot_id)

        async def runner() -> Any:
            async with lock:
                return await fn()

        return await asyncio.shield(asyncio.ensure_future(runner()))

    def _changed(self, rt: BotRuntime) -> None:
        if rt.bot_id in self.runtimes:
            try:
                self.on_change(rt.bot_id)
            except Exception:
                log.exception("on_change callback failed")

    def mark_changed(self, bot_id: str) -> None:
        """Code/env/settings changed: flag a running process as stale."""
        rt = self.runtimes.get(bot_id)
        if rt is not None and rt.state in ("installing", "starting", "running") and rt.mode == "run":
            if not rt.needs_restart:
                rt.needs_restart = True
                self._changed(rt)

    def entry_error(self, bot: dict) -> str | None:
        ep = bot.get("entrypoint")
        if not ep:
            return "No entrypoint set"
        if not (self.settings.src_dir(bot["id"]) / ep).is_file():
            return f"Entrypoint {ep} not found"
        return None

    def fingerprint(self, bot: dict) -> str:
        h = hashlib.sha256()
        req = self.settings.src_dir(bot["id"]) / "requirements.txt"
        try:
            h.update(req.read_bytes())
        except OSError:
            h.update(b"<none>")
        h.update(b"\0" + (bot.get("extra_packages") or "").encode())
        h.update(b"\0" + sys.version.encode())
        return h.hexdigest()

    def install_needed(self, bot: dict) -> bool:
        if not self.settings.use_venv:
            return False
        if not venv_python(self.settings.venv_dir(bot["id"])).exists():
            return True
        return self.fingerprint(bot) != bot.get("deps_fingerprint")

    def base_env(self, venv: Path | None) -> dict[str, str]:
        src = os.environ
        env: dict[str, str] = {}
        path = src.get("PATH", "")
        if venv is not None:
            path = str(venv_bin(venv)) + (os.pathsep + path if path else "")
            env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = path
        home = src.get("HOME") or (src.get("USERPROFILE") if IS_WINDOWS else None)
        if not home:
            home_dir = self.settings.data_dir / "home"
            home_dir.mkdir(parents=True, exist_ok=True)
            home = str(home_dir)
        env["HOME"] = home
        env["LANG"] = src.get("LANG") or "C.UTF-8"
        if src.get("TZ"):
            env["TZ"] = src["TZ"]
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if IS_WINDOWS:
            for key in WINDOWS_ENV_KEEP:
                if key in src:
                    env[key] = src[key]
        return env

    def bot_env(self, bot: dict) -> dict[str, str]:
        venv = self.settings.venv_dir(bot["id"]) if self.settings.use_venv else None
        env = self.base_env(venv)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for row in self.db.env_rows(bot["id"]):
            if row["key"].upper().startswith("BOTDOCK_"):
                continue
            env[row["key"]] = self.crypto.decrypt(row["value_enc"])
        return {k: v for k, v in env.items() if not k.upper().startswith("BOTDOCK_")}

    def python_for(self, bot: dict) -> str:
        if self.settings.use_venv:
            return str(venv_python(self.settings.venv_dir(bot["id"])))
        return sys.executable

    # public operations --------------------------------------------------------------
    async def start(self, bot_id: str, manual: bool = True) -> None:
        await self.serialized(bot_id, lambda: self.start_locked(bot_id, manual))

    async def stop(self, bot_id: str) -> None:
        await self.serialized(bot_id, lambda: self.stop_locked(bot_id, set_desired=True))

    async def restart(self, bot_id: str) -> None:
        async def op() -> None:
            bot = self._bot(bot_id)
            err = self.entry_error(bot)
            if err:
                raise ApiError(409, err)
            rt = self.runtime(bot_id)
            await self._cancel_task(rt, reason="Restarting")
            await self.start_locked(bot_id, manual=True)

        await self.serialized(bot_id, op)

    async def reinstall(self, bot_id: str) -> None:
        async def op() -> None:
            self._bot(bot_id)
            rt = self.runtime(bot_id)
            was_running = rt.task_alive() and rt.mode == "run"
            await self._cancel_task(rt, reason="Reinstalling dependencies")
            rt.state = "stopped"
            rt.next_restart_at = None
            self.db.update_bot(bot_id, {"deps_fingerprint": None}, touch=False)
            if not self.settings.use_venv:
                rt.system("Virtualenvs are disabled (BOTDOCK_USE_VENV=0); nothing to reinstall")
            else:
                rt.system("Removing virtual environment…")
                try:
                    await asyncio.to_thread(rmtree_retry, self.settings.venv_dir(bot_id))
                except OSError as exc:
                    raise ApiError(409, f"Could not remove the virtualenv: {exc}") from exc
            if was_running:
                await self.start_locked(bot_id, manual=False)
            elif self.settings.use_venv:
                rt.mode = "install"
                rt.state = "installing"
                rt.last_error = None
                rt.task = asyncio.get_running_loop().create_task(self._run(rt, install_only=True))
            self._changed(rt)

        await self.serialized(bot_id, op)

    def _bot(self, bot_id: str) -> dict:
        bot = self.db.get_bot(bot_id)
        if bot is None:
            raise ApiError(404, "Bot not found")
        return bot

    async def start_locked(self, bot_id: str, manual: bool) -> None:
        if self.shutting_down:
            raise ApiError(409, "BotDock is shutting down")
        bot = self._bot(bot_id)
        rt = self.runtime(bot_id)
        if rt.task_alive() and rt.mode == "run" and rt.next_restart_at is None:
            if manual:  # already installing/starting/running: idempotent
                self.db.update_bot(bot_id, {"desired_state": "running"}, touch=False)
            return
        err = self.entry_error(bot)
        if err:
            if manual:
                raise ApiError(409, err)
            rt.state = "crashed"
            rt.last_error = err
            rt.system(err)
            self._changed(rt)
            return
        await self._cancel_task(rt, reason=None)  # pending backoff restart or install-only task
        if manual:
            self.db.update_bot(bot_id, {"desired_state": "running"}, touch=False)
        rt.restart_count = 0
        rt.rapid_failures = 0
        rt.last_error = None
        rt.next_restart_at = None
        rt.mode = "run"
        rt.state = "installing" if self.install_needed(bot) else "starting"
        rt.task = asyncio.get_running_loop().create_task(self._run(rt))
        self._changed(rt)

    async def after_update_locked(self, bot_id: str, reason: str) -> None:
        """After new code landed (GitHub sync), with the bot lock held: restart a bot that is
        installing/starting/running, start a crashed one, leave a stopped one alone. Desired state
        is not changed."""
        rt = self.runtime(bot_id)
        active = rt.task_alive() and rt.mode == "run" and rt.next_restart_at is None
        if active:
            await self._cancel_task(rt, reason=f"Restarting: {reason}")
            await self.start_locked(bot_id, manual=False)
        elif rt.state == "crashed":
            rt.system(f"Starting: {reason}")
            await self.start_locked(bot_id, manual=False)

    async def stop_locked(self, bot_id: str, set_desired: bool) -> None:
        bot = self.db.get_bot(bot_id)
        if set_desired:
            if bot is None:
                raise ApiError(404, "Bot not found")
            self.db.update_bot(bot_id, {"desired_state": "stopped"}, touch=False)
        rt = self.runtimes.get(bot_id)
        if rt is None:
            return
        await self._cancel_task(rt, reason="Stopping")
        rt.state = "stopped"
        rt.next_restart_at = None
        rt.needs_restart = False
        self._changed(rt)

    async def _cancel_task(self, rt: BotRuntime, reason: str | None) -> None:
        task = rt.task
        if task is None or task.done():
            return
        if rt.proc is not None:
            rt.state = "stopping"
            if reason:
                rt.system(f"{reason}…")
            self._changed(rt)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if cur is not None and cur.cancelling():
                raise
        except Exception:
            log.exception("bot task failed while being cancelled")

    # the run task -------------------------------------------------------------------
    def _fail(self, rt: BotRuntime, message: str) -> None:
        rt.state = "crashed"
        rt.last_error = message
        rt.next_restart_at = None
        rt.system(message)
        self._changed(rt)

    async def _run(self, rt: BotRuntime, install_only: bool = False) -> None:
        s = self.settings
        try:
            while True:
                bot = self.db.get_bot(rt.bot_id)
                if bot is None:
                    return
                if not await self._prepare(rt, bot):
                    return
                if install_only:
                    rt.state = "stopped"
                    self._changed(rt)
                    return
                bot = self.db.get_bot(rt.bot_id)
                if bot is None:
                    return
                err = self.entry_error(bot)
                if err:
                    self._fail(rt, err)
                    return
                try:
                    env = self.bot_env(bot)
                except InvalidToken:
                    self._fail(rt, "Cannot decrypt env vars (was the secret key changed?)")
                    return
                try:
                    args = shlex.split(bot["args"] or "")
                except ValueError:
                    self._fail(rt, "Invalid args (unbalanced quotes)")
                    return
                cmd = [self.python_for(bot), "-u", bot["entrypoint"], *args]
                rt.state = "starting"
                rt.needs_restart = False
                rt.system("Starting: python -u " + " ".join([bot["entrypoint"], *args]))
                try:
                    handle = ProcessHandle(self.loop, cmd, str(s.src_dir(rt.bot_id)), env,
                                           ("stdout", "stderr"), rt.deliver, rt.writer)
                except OSError as exc:
                    self._fail(rt, f"Could not start process: {exc}")
                    return
                rt.proc = handle
                rt.pid = handle.pid
                rt.started_at = time.time()
                self._changed(rt)
                try:
                    rc = await asyncio.wait_for(asyncio.shield(handle.exit_future), s.startup_grace)
                except TimeoutError:
                    rt.state = "running"
                    rt.last_error = None
                    self._changed(rt)
                    rc = await asyncio.shield(handle.exit_future)
                uptime = time.time() - (rt.started_at or time.time())
                rt.proc = None
                rt.pid = None
                rt.started_at = None
                rt.exit_code = rc
                rt.cpu_percent = rt.memory_bytes = None
                rt.system(f"Process exited with code {rc}")

                bot = self.db.get_bot(rt.bot_id)
                if bot is None:
                    return
                policy = bot["restart_policy"]
                restart = policy == "always" or (policy == "on-failure" and rc != 0)
                if not restart or self.shutting_down:
                    rt.state = "stopped" if rc == 0 else "crashed"
                    rt.last_error = None if rc == 0 else f"Process exited with code {rc}"
                    self._changed(rt)
                    return
                if uptime >= s.stable_uptime:
                    rt.rapid_failures = 0
                    n = 1
                else:
                    rt.rapid_failures += 1
                    n = rt.rapid_failures
                    if n >= s.max_rapid_failures:
                        self._fail(rt, f"Crashed {n} times in a row; giving up (last exit code {rc})")
                        return
                delay = min(s.backoff_base * (2 ** (n - 1)), s.backoff_max)
                rt.state = "crashed"
                rt.last_error = f"Process exited with code {rc}"
                rt.next_restart_at = time.time() + delay
                rt.system(f"Restarting in {delay:g}s")
                self._changed(rt)
                await asyncio.sleep(delay)
                rt.next_restart_at = None
                rt.restart_count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("bot %s run task failed", rt.bot_id)
            self._fail(rt, f"Internal error: {exc}")
        finally:
            handle = rt.proc
            if handle is not None:
                grace = s.shutdown_stop_timeout if self.shutting_down else s.stop_timeout
                rc = await terminate_tree(handle, grace)
                rt.exit_code = rc
                rt.system(f"Process stopped (exit code {rc})")
            rt.proc = None
            rt.pid = None
            rt.started_at = None
            rt.cpu_percent = rt.memory_bytes = None

    async def _run_logged(self, rt: BotRuntime, cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
        handle = ProcessHandle(self.loop, cmd, str(cwd), env, ("system", "system"), rt.deliver, rt.writer)
        rt.proc = handle
        try:
            return await asyncio.shield(handle.exit_future)
        finally:
            if handle.exited:
                rt.proc = None

    async def _prepare(self, rt: BotRuntime, bot: dict) -> bool:
        """Ensure venv + dependencies. Returns False (state crashed) on failure."""
        if not self.settings.use_venv or not self.install_needed(bot):
            return True
        bot_id = bot["id"]
        venv = self.settings.venv_dir(bot_id)
        py = venv_python(venv)
        src = self.settings.src_dir(bot_id)
        rt.state = "installing"
        self._changed(rt)
        env = self.base_env(None)
        env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
        try:
            if not py.exists():
                if venv.exists():
                    await asyncio.to_thread(rmtree_retry, venv)
                self.db.update_bot(bot_id, {"deps_fingerprint": None}, touch=False)
                rt.system("Creating virtual environment…")
                rc = await self._run_logged(rt, [sys.executable, "-m", "venv", str(venv)], venv.parent, env)
                if rc != 0 or not py.exists():
                    self._fail(rt, f"Virtualenv creation failed (exit {rc})")
                    return False
            fp = self.fingerprint(bot)
            venv_env = self.base_env(venv)
            venv_env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
            if (src / "requirements.txt").is_file():
                rt.system("Installing dependencies from requirements.txt…")
                rc = await self._run_logged(
                    rt, [str(py), "-m", "pip", "install", "-r", "requirements.txt"], src, venv_env)
                if rc != 0:
                    self._fail(rt, f"Dependency install failed (exit {rc})")
                    return False
            extra = (bot.get("extra_packages") or "").split()
            if extra:
                rt.system("Installing extra packages: " + " ".join(extra))
                rc = await self._run_logged(rt, [str(py), "-m", "pip", "install", *extra], src, venv_env)
                if rc != 0:
                    self._fail(rt, f"Extra package install failed (exit {rc})")
                    return False
        except OSError as exc:
            self._fail(rt, f"Dependency install failed: {exc}")
            return False
        self.db.update_bot(bot_id, {"deps_fingerprint": fp}, touch=False)
        rt.system("Dependencies ready")
        return True

    # stats / serialisation ----------------------------------------------------------
    def refresh_stats(self) -> None:
        for rt in self.runtimes.values():
            if rt.state != "running" or rt.pid is None:
                rt.cpu_percent = rt.memory_bytes = None
                rt._ps.clear()
                continue
            try:
                root = rt._ps.get(rt.pid) or psutil.Process(rt.pid)
                procs = [root] + root.children(recursive=True)
            except psutil.Error:
                continue
            cpu, mem, seen = 0.0, 0, {}
            for p in procs:
                p = rt._ps.get(p.pid, p)
                try:
                    cpu += p.cpu_percent(None)
                    mem += p.memory_info().rss
                    seen[p.pid] = p
                except psutil.Error:
                    pass
            rt._ps = seen
            rt.cpu_percent = round(cpu, 1)
            rt.memory_bytes = mem

    def runtime_json(self, bot_id: str) -> dict:
        rt = self.runtimes.get(bot_id)
        if rt is None:
            return {"state": "stopped", "pid": None, "started_at": None, "exit_code": None, "restart_count": 0,
                    "next_restart_at": None, "cpu_percent": None, "memory_bytes": None, "last_error": None}
        running = rt.state == "running"
        if running and rt.cpu_percent is None:
            self.refresh_stats()
        return {
            "state": rt.state,
            "pid": rt.pid,
            "started_at": iso(rt.started_at) if running else None,
            "exit_code": rt.exit_code,
            "restart_count": rt.restart_count,
            "next_restart_at": iso(rt.next_restart_at),
            "cpu_percent": rt.cpu_percent if running else None,
            "memory_bytes": rt.memory_bytes if running else None,
            "last_error": rt.last_error,
        }

    def needs_restart(self, bot_id: str) -> bool:
        rt = self.runtimes.get(bot_id)
        return bool(rt and rt.needs_restart and rt.state in ("installing", "starting", "running"))

    def running_count(self) -> int:
        return sum(1 for rt in self.runtimes.values() if rt.state == "running")
