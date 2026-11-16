"""End-to-end: run ``python -m botdock`` as a real process, drive it over HTTP, shut it down with a
signal and verify the bot process tree is gone."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import psutil
import pytest

from conftest import ROOT, wait_for

BOT = b"""
import os, time
from pathlib import Path
print("token is", "set" if os.getenv("DISCORD_TOKEN") else "missing", flush=True)
i = 0
while True:
    i += 1
    print("beat", i, flush=True)
    time.sleep(0.2)
"""


SECRET = "s3cr3t-V4LUE-do-not-log"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOTDOCK_")}
    env.update(BOTDOCK_DATA=str(tmp_path / "data"), BOTDOCK_HOST="127.0.0.1", BOTDOCK_PORT=str(port),
               BOTDOCK_USE_VENV="0", PYTHONUNBUFFERED="1")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    log_path = tmp_path / "server.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, "-m", "botdock"], cwd=str(ROOT), env=env, stdout=log,
                            stderr=subprocess.STDOUT, **kwargs)
    base = f"http://127.0.0.1:{port}"

    def up():
        try:
            return httpx.get(base + "/api/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        wait_for(up, timeout=30, desc="server up")
        yield proc, base, log_path
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()


def stop_server(proc):
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=30)


def test_real_server_flow(server):
    proc, base, log_path = server
    with httpx.Client(base_url=base, headers={"X-BotDock": "1"}, timeout=10) as c:
        assert c.get("/api/auth/state").json()["setup_code_required"] is True
        code = (log_path.parent / "data" / "setup-code.txt").read_text().strip()
        assert code in log_path.read_text(errors="replace")  # printed to the container log
        assert c.post("/api/auth/setup", json={"password": "password123"}).status_code == 403
        assert c.post("/api/auth/setup", json={"password": "password123", "setup_code": code}).status_code == 200
        assert not (log_path.parent / "data" / "setup-code.txt").exists()
        r = c.post("/api/bots", files=[("files", ("beat.py", BOT, "text/x-python"))], data={"name": "Beat"})
        assert r.status_code == 201, r.text
        bot_id = r.json()["bot"]["id"]
        assert c.put(f"/api/bots/{bot_id}/env/DISCORD_TOKEN", json={"value": SECRET}).status_code == 200
        assert c.post(f"/api/bots/{bot_id}/start").status_code == 200
        wait_for(lambda: c.get(f"/api/bots/{bot_id}").json()["bot"]["runtime"]["state"] == "running", timeout=20)
        pid = c.get(f"/api/bots/{bot_id}").json()["bot"]["runtime"]["pid"]
        tree = [pid] + [p.pid for p in psutil.Process(pid).children(recursive=True)]

        got = []
        with c.stream("GET", f"/api/bots/{bot_id}/logs/stream?after=0") as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    got.append(json.loads(line[6:])["text"])
                    if sum(t.startswith("beat") for t in got) >= 3:
                        break
        assert "token is set" in got

        # leave an events stream open: shutdown must not hang on it
        events_cm = c.stream("GET", "/api/events")
        events = events_cm.__enter__()
        assert events.status_code == 200

        t0 = time.monotonic()
        rc = stop_server(proc)
        assert time.monotonic() - t0 < 20, "shutdown hung"
        events_cm.__exit__(None, None, None)
    output = log_path.read_bytes().decode("utf-8", "replace")
    assert SECRET not in output  # secret value never logged by BotDock
    for p in tree:
        assert not psutil.pid_exists(p) or psutil.Process(p).status() == psutil.STATUS_ZOMBIE, output
    assert rc is not None


def _send_streaming(base: str, path: str, total_mb: int, headers: dict) -> tuple[int | None, int]:
    """POST a chunked body of total_mb MB; returns (status or None if the server hung up, bytes sent)."""
    sent = [0]

    def gen():
        for _ in range(total_mb * 16):
            sent[0] += 65536
            yield b" " * 65536

    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(base + path, content=gen(), headers=headers)
            return r.status_code, sent[0]
    except httpx.HTTPError:
        return None, sent[0]


def test_real_server_rejects_big_and_unauthenticated_bodies_early(server, tmp_path):
    proc, base, _ = server
    real = psutil.Process(proc.pid)
    procs = [real] + real.children(recursive=True)
    rss_before = max(p.memory_info().rss for p in procs)
    status, sent = _send_streaming(base, "/api/auth/login", 400,
                                   {"X-BotDock": "1", "content-type": "application/json"})
    assert status in (413, None), status
    assert sent < 64 * 1024 * 1024, f"server kept reading: {sent >> 20} MB"
    status, sent = _send_streaming(base, "/api/bots", 400,
                                   {"X-BotDock": "1", "content-type": "multipart/form-data; boundary=b"})
    assert status in (401, None), status
    assert sent < 64 * 1024 * 1024, f"server kept reading: {sent >> 20} MB"
    rss_after = max(p.memory_info().rss for p in procs if p.is_running())
    assert rss_after - rss_before < 40 * 1024 * 1024
    tmp = tmp_path / "data" / "tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []
    assert httpx.get(base + "/api/health").status_code == 200
    stop_server(proc)
