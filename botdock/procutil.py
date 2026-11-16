"""Cross-platform child process handling.

Processes are spawned with ``subprocess.Popen`` and watched by plain threads (one reader per
pipe, one waiter). That works identically on Windows and Linux and with any asyncio event loop
(Proactor or Selector), never blocks the loop, and guarantees the tail of the output is delivered
before the exit is reported: the waiter joins the readers (which flush their partial last line on
EOF) before resolving ``exit_future``.

Each child gets its own process group / session so a Ctrl+C aimed at BotDock does not hit the
bots directly, and stop can signal the whole tree.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from typing import Callable

import psutil

from .logstore import LogWriter
from .util import now_iso

MAX_LINE_BYTES = 16 * 1024
IS_WINDOWS = os.name == "nt"

Deliver = Callable[[str, list[tuple[str, str]]], None]


class ProcessHandle:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        streams: tuple[str, str],
        deliver: Deliver,
        writer: LogWriter,
    ):
        self.loop = loop
        self.deliver = deliver
        self.writer = writer
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self.popen = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        self.pid = self.popen.pid
        self.exit_future: asyncio.Future[int] = loop.create_future()
        self._readers = [
            threading.Thread(target=self._read, args=(self.popen.stdout, streams[0]), daemon=True,
                             name=f"botdock-out-{self.pid}"),
            threading.Thread(target=self._read, args=(self.popen.stderr, streams[1]), daemon=True,
                             name=f"botdock-err-{self.pid}"),
        ]
        for t in self._readers:
            t.start()
        threading.Thread(target=self._wait, daemon=True, name=f"botdock-wait-{self.pid}").start()

    # threads ------------------------------------------------------------------------
    def _read(self, pipe, stream: str) -> None:
        buf = b""
        try:
            while True:
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    parts = buf.split(b"\n")
                    buf = parts.pop()
                    self._emit(stream, parts)
                while len(buf) > MAX_LINE_BYTES:
                    self._emit(stream, [buf[:MAX_LINE_BYTES]])
                    buf = buf[MAX_LINE_BYTES:]
        except (OSError, ValueError):
            pass
        finally:
            if buf:
                self._emit(stream, [buf])
            try:
                pipe.close()
            except OSError:
                pass

    def _emit(self, stream: str, raw_lines: list[bytes]) -> None:
        ts = now_iso(millis=True)
        items = [(ts, raw.decode("utf-8", "replace").rstrip("\r")) for raw in raw_lines]
        self.writer.write([(ts, stream, text) for ts, text in items])
        try:
            self.loop.call_soon_threadsafe(self.deliver, stream, items)
        except RuntimeError:  # loop closed
            pass

    def _wait(self) -> None:
        rc = self.popen.wait()
        for t in self._readers:
            t.join(timeout=2.0)
        try:
            self.loop.call_soon_threadsafe(self._set_exit, rc)
        except RuntimeError:
            pass

    def _set_exit(self, rc: int) -> None:
        if not self.exit_future.done():
            self.exit_future.set_result(rc)

    @property
    def exited(self) -> bool:
        return self.exit_future.done()


def process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root] + root.children(recursive=True)
    except psutil.Error:
        return []


def _alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


async def terminate_tree(handle: ProcessHandle, timeout: float) -> int | None:
    """Gracefully stop the process and all its descendants; force-kill after ``timeout``."""
    pid = handle.pid
    procs = [p for p in process_tree(pid) if p.pid != pid] if not handle.exited else []
    if not IS_WINDOWS:
        try:  # the group outlives its leader while any member is alive, so this is safe
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for p in procs:  # descendants that left the process group
            try:
                if os.getpgid(p.pid) != pid:
                    p.terminate()
            except (psutil.Error, OSError):
                pass
    else:
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        if not handle.exited:
            try:
                handle.popen.terminate()
            except OSError:
                pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.exited and not any(_alive(p) for p in procs):
            break
        await asyncio.sleep(0.05)
    else:
        if not IS_WINDOWS:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for p in procs:
            try:
                if _alive(p):
                    p.kill()
            except psutil.Error:
                pass
        if not handle.exited:
            try:
                handle.popen.kill()
            except OSError:
                pass
    try:
        return await asyncio.wait_for(asyncio.shield(handle.exit_future), 3.0)
    except TimeoutError:
        return None
