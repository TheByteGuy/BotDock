"""Per-bot log files (with rotation), in-memory ring buffers and subscriber queues."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections import deque
from pathlib import Path
from typing import Iterator

_LINE_RE = re.compile(r"^(\S+) \[(stdout|stderr|system)\] (.*)$")
TAIL_READ_BYTES = 4 * 1024 * 1024


class Subscriber:
    """Unbounded-ish queue that a producer on the event loop thread feeds without blocking."""

    MAX_PENDING = 20_000

    def __init__(self) -> None:
        self.items: deque = deque()
        self.event = asyncio.Event()
        self.closed = False
        self.revoked = False  # session ended: stop without delivering what is still queued

    def push(self, item) -> None:
        if self.closed:
            return
        if len(self.items) >= self.MAX_PENDING:  # consumer too slow; make it reconnect
            self.close()
            return
        self.items.append(item)
        self.event.set()

    def close(self) -> None:
        self.closed = True
        self.event.set()

    async def wait(self, timeout: float) -> bool:
        """Wait for items (or close). Returns False on timeout."""
        if self.items or self.closed:
            return True
        self.event.clear()
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def drain(self) -> list:
        out = list(self.items)
        self.items.clear()
        self.event.clear()
        return out


class LogWriter:
    """Appends formatted lines to logs/bot.log, rotating to .1 .2 .3 at max_bytes. Thread-safe."""

    def __init__(self, logs_dir: Path, max_bytes: int, backups: int):
        self.dir = Path(logs_dir)
        self.path = self.dir / "bot.log"
        self.max_bytes = max_bytes
        self.backups = backups
        self._fh = None
        self._size = 0
        self._lock = threading.Lock()
        self._closed = False

    def _open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")
        self._size = self._fh.tell()

    def _rotate(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        try:
            for i in range(self.backups, 0, -1):
                src = self.path if i == 1 else self.path.with_name(f"bot.log.{i - 1}")
                dst = self.path.with_name(f"bot.log.{i}")
                if src.exists():
                    os.replace(src, dst)
        except OSError:
            pass  # e.g. file briefly locked on Windows; keep appending, retry next time

    def write(self, items: list[tuple[str, str, str]]) -> None:
        """items: (ts, stream, text)"""
        if not items:
            return
        data = "".join(f"{ts} [{stream}] {text}\n" for ts, stream, text in items).encode("utf-8", "replace")
        with self._lock:
            if self._closed:
                return
            try:
                if self._fh is None:
                    self._open()
                if self._size > 0 and self._size + len(data) > self.max_bytes:
                    self._rotate()
                    self._open()
                self._fh.write(data)
                self._fh.flush()
                self._size += len(data)
            except OSError:
                pass

    def files_oldest_first(self) -> list[Path]:
        paths = [self.path.with_name(f"bot.log.{i}") for i in range(self.backups, 0, -1)] + [self.path]
        return [p for p in paths if p.exists()]

    def iter_bytes(self) -> Iterator[bytes]:
        for p in self.files_oldest_first():
            try:
                with open(p, "rb") as fh:
                    while chunk := fh.read(256 * 1024):
                        yield chunk
            except OSError:
                continue

    def read_tail(self, n: int) -> list[tuple[str, str, str]]:
        """Last n parsed lines across rotations (oldest first)."""
        collected: list[tuple[str, str, str]] = []
        for p in reversed(self.files_oldest_first()):
            try:
                size = p.stat().st_size
                with open(p, "rb") as fh:
                    start = max(0, size - TAIL_READ_BYTES)
                    fh.seek(start)
                    data = fh.read()
            except OSError:
                continue
            lines = data.decode("utf-8", "replace").split("\n")
            if start > 0:
                lines = lines[1:]  # first line is probably partial
            parsed = []
            for line in lines:
                if not line:
                    continue
                m = _LINE_RE.match(line)
                if m:
                    parsed.append((m.group(1), m.group(2), m.group(3)))
                else:
                    parsed.append((parsed[-1][0] if parsed else "", "system", line))
            collected = parsed + collected
            if len(collected) >= n or start > 0:
                break
        return collected[-n:]

    def clear(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            for p in self.files_oldest_first():
                try:
                    p.unlink()
                except OSError:
                    pass
            self._size = 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fh is not None:
                self._fh.close()
                self._fh = None
