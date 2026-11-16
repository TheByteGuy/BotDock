"""Fixes from the frontend run: SSE vs session revocation, export skip list, bytecode, name rules."""

from __future__ import annotations

import io
import json
import os
import threading
import time
import zipfile

import httpx
import pytest

from conftest import setup_body, PASSWORD, create_bot, get_bot, log_texts, wait_for, wait_state
from fake_github import FakeGitHub

TICKER = b"import time\ni = 0\nwhile True:\n    i += 1\n    print('tick', i, flush=True)\n    time.sleep(0.1)\n"


def two_sessions(client):
    client.post("/api/auth/setup", json=setup_body(client, PASSWORD))
    a = client.cookies["botdock_session"]
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": PASSWORD})
    b = client.cookies["botdock_session"]
    client.cookies.clear()
    assert a != b
    return {"Cookie": f"botdock_session={a}"}, {"Cookie": f"botdock_session={b}"}


class StreamReader(threading.Thread):
    def __init__(self, client, url, headers):
        super().__init__(daemon=True)
        self.client, self.url, self.headers = client, url, headers
        self.events: list[tuple[float, str]] = []
        self.ended_at: float | None = None

    def run(self):
        deadline = time.monotonic() + 20
        with self.client.stream("GET", self.url, headers=self.headers) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data: "):
                    self.events.append((time.monotonic(), line))
                if time.monotonic() > deadline:
                    break
        self.ended_at = time.monotonic()


def start_ticker(client, hdr):
    r = client.post("/api/bots", files=[("files", ("t.py", TICKER))], data={"name": "T"}, headers=hdr)
    bot_id = r.json()["bot"]["id"]
    client.post(f"/api/bots/{bot_id}/start", headers=hdr)
    wait_for(lambda: client.get(f"/api/bots/{bot_id}", headers=hdr).json()["bot"]["runtime"]["state"] == "running")
    return bot_id


@pytest.mark.parametrize("how", ["password_change", "logout", "expired"])
def test_streams_end_when_session_ends(client, how):
    a, b = two_sessions(client)
    bot_id = start_ticker(client, b)
    logs = StreamReader(client, f"/api/bots/{bot_id}/logs/stream", a)
    events = StreamReader(client, "/api/events", a)
    logs.start()
    events.start()
    wait_for(lambda: len(logs.events) >= 3 and len(events.events) >= 1, desc="streams flowing")
    t = time.monotonic()
    if how == "password_change":
        r = client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "another-pass1"},
                        headers=b)
        assert r.status_code == 200
    elif how == "logout":
        assert client.post("/api/auth/logout", headers=a).status_code == 200
    else:  # session expires / disappears without going through the API
        client.app.state.ctx.db.execute("DELETE FROM sessions WHERE expires_at > 0 AND token_hash IN "
                                        "(SELECT token_hash FROM sessions)")
    logs.join(5)
    events.join(5)
    assert logs.ended_at is not None and events.ended_at is not None, "stream kept running after revocation"
    limit = 0.5 if how != "expired" else 3.0
    assert logs.ended_at - t < limit + 0.5
    assert not [e for e in logs.events if e[0] > t + limit], "log lines delivered after revocation"
    assert client.get("/api/auth/state", headers=a).json()["authenticated"] is False
    if how == "password_change":  # the session that changed the password keeps its streams
        assert client.get(f"/api/bots/{bot_id}/logs?tail=1", headers=b).status_code == 200
    client.cookies.clear()


def test_export_skips_caches_and_bots_write_no_bytecode(authed, data_dir):
    script = b"import os, helper\nprint('DWB', os.environ.get('PYTHONDONTWRITEBYTECODE'))\n"
    bot_id = create_bot(authed, "Ex", [("p/main.py", script), ("p/helper.py", b"X = 1\n")])["bot"]["id"]
    authed.patch(f"/api/bots/{bot_id}", json={"restart_policy": "never"})
    authed.post(f"/api/bots/{bot_id}/start")
    wait_state(authed, bot_id, "stopped")
    assert "DWB 1" in log_texts(authed, bot_id)
    src = data_dir / "bots" / bot_id / "src"
    assert not (src / "__pycache__").exists()  # importing helper wrote no bytecode
    # caches that got there anyway are not exported
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "helper.cpython-312.pyc").write_bytes(b"\0")
    (src / "stray.pyc").write_bytes(b"\0")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    names = zipfile.ZipFile(io.BytesIO(authed.get(f"/api/bots/{bot_id}/export").content)).namelist()
    assert sorted(names) == ["helper.py", "main.py"]


@pytest.mark.usefixtures("posix_rules")
@pytest.mark.skipif(os.name == "nt", reason="aux.py / ':' names can't be created on Windows")
def test_linux_repo_with_windows_reserved_names_syncs(make_client, data_dir):
    gh = FakeGitHub()
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app))
    c.post("/api/auth/setup", json=setup_body(c, PASSWORD))
    gh.add_repo("octo/bot", {"main.py": "x", "lib/aux.py": "a", "con.txt": "c", "a:b.txt": "d"})
    c.post("/api/github/token", json={"token": "good-token"})
    r = c.post("/api/bots/github", json={"name": "Lin", "repo": "octo/bot"})
    assert r.status_code == 201, r.text
    src = data_dir / "bots" / r.json()["bot"]["id"] / "src"
    assert (src / "lib" / "aux.py").read_text() == "a" and (src / "a:b.txt").exists()


def test_linking_while_disconnected_reports_error(make_client):
    gh = FakeGitHub()
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app))
    c.post("/api/auth/setup", json=setup_body(c, PASSWORD))
    gh.add_repo("octo/bot", {"main.py": "x"})
    bot_id = create_bot(c, "Up", [("main.py", b"")])["bot"]["id"]
    r = c.put(f"/api/bots/{bot_id}/source", json={"type": "github", "repo": "octo/bot", "branch": "main"})
    assert r.status_code == 200
    assert r.json()["bot"]["source"]["last_error"] == "GitHub is not connected"
    c.post("/api/github/token", json={"token": "good-token"})
    assert get_bot(c, bot_id)["source"]["last_error"] is None
