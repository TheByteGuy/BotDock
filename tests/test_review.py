"""Reviewer findings A-G."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from botdock.github import compile_keep, prune_backups
from conftest import PASSWORD, create_bot, get_bot, log_texts, setup_body, upload, wait_for, wait_state
from fake_github import FakeGitHub


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def ghc(make_client, gh):
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app))
    assert c.post("/api/auth/setup", json=setup_body(c, PASSWORD)).status_code == 200
    return c


def poll_once(c):
    c.portal.call(c.app.state.ctx.github.poll_once)


def sync_and_wait(c, bot_id):
    assert c.post(f"/api/bots/{bot_id}/source/sync").status_code == 200
    return wait_for(lambda: (b := get_bot(c, bot_id))["source"]["syncing"] is False and b, desc="sync done")


# A ------------------------------------------------------------------------------------------------


def test_linking_never_auto_pulls_then_manual_sync_backs_up(ghc, gh, data_dir):
    gh.add_repo("octo/ticketbot", {"main.py": "repo main", "data/tickets.json": "repo defaults"})
    bot_id = create_bot(ghc, "Tickets", [("tb/main.py", b"local main"), ("tb/data/tickets.json", b"local data")])["bot"]["id"]
    src = data_dir / "bots" / bot_id / "src"
    ghc.post("/api/github/token", json={"token": "good-token"})
    r = ghc.put(f"/api/bots/{bot_id}/source", json={"type": "github", "repo": "octo/ticketbot", "branch": "main",
                                                       "auto_update": True})
    assert r.status_code == 200 and r.json()["bot"]["source"]["keep_paths"] == []
    bot = wait_for(lambda: (b := get_bot(ghc, bot_id))["source"]["latest_sha"] and b, desc="background check")
    s = bot["source"]
    assert s["commit_sha"] is None and s["update_available"] is True  # "not pulled yet"
    for _ in range(3):
        poll_once(ghc)
    assert (src / "main.py").read_text() == "local main"
    assert (src / "data/tickets.json").read_text() == "local data"
    assert not [e for e in gh.log if "legacy.tar.gz" in e["path"]], "poller downloaded the repo"
    assert get_bot(ghc, bot_id)["source"]["commit_sha"] is None

    # an explicit pull does the first sync, keeping the overwritten files as a backup
    bot = sync_and_wait(ghc, bot_id)
    assert bot["source"]["commit_sha"] and bot["source"]["last_error"] is None
    assert (src / "main.py").read_text() == "repo main"
    backups = list((data_dir / "bots" / bot_id / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "main.py").read_text() == "local main"
    assert (backups[0] / "data" / "tickets.json").read_text() == "local data"
    lines = [t for t in log_texts(ghc, bot_id) if t.startswith("Backed up")]
    assert len(lines) == 1 and f"backups/{backups[0].name}/" in lines[0]

    # later auto-updates do run (commit_sha is established now)
    gh.push("octo/ticketbot", {"main.py": "repo main v2"})
    poll_once(ghc)
    assert (src / "main.py").read_text() == "repo main v2"
    assert len(list((data_dir / "bots" / bot_id / "backups").iterdir())) == 1  # nothing local was modified


def test_keep_paths(ghc, gh, data_dir):
    gh.add_repo("octo/bot", {"main.py": "v1", "data/tickets.json": "repo", "config.py": "cfg1", "data/old.json": "o"})
    ghc.post("/api/github/token", json={"token": "good-token"})
    assert ghc.post("/api/bots/github", json={"name": "Bad", "repo": "octo/bot",
                                              "keep_paths": ["../x"]}).status_code == 400
    assert ghc.post("/api/bots/github", json={"name": "Bad", "repo": "octo/bot", "keep_paths": "data"}).status_code == 400
    r = ghc.post("/api/bots/github", json={"name": "Keep", "repo": "octo/bot", "auto_update": False,
                                          "keep_paths": ["data/**", "config.py"]})
    assert r.status_code == 201, r.text
    bot = r.json()["bot"]
    assert bot["source"]["keep_paths"] == ["data/**", "config.py"]
    src = data_dir / "bots" / bot["id"] / "src"
    assert (src / "data/tickets.json").read_text() == "repo"  # created: did not exist locally yet
    (src / "data/tickets.json").write_text("runtime")
    gh.push("octo/bot", {"main.py": "v2", "data/tickets.json": "repo v2", "config.py": "cfg2",
                         "data/new.json": "new"}, delete=("data/old.json",))
    sync_and_wait(ghc, bot["id"])
    assert (src / "main.py").read_text() == "v2"
    assert (src / "data/tickets.json").read_text() == "runtime"
    assert (src / "config.py").read_text() == "cfg1"
    assert (src / "data/new.json").read_text() == "new"
    assert (src / "data/old.json").exists()  # kept paths are never deleted
    assert not (data_dir / "bots" / bot["id"] / "backups").exists()
    assert any("keep_paths" in t for t in log_texts(ghc, bot["id"]))
    # changing keep_paths via PUT source
    r = ghc.put(f"/api/bots/{bot['id']}/source", json={"type": "github", "repo": "octo/bot", "branch": "main",
                                                         "keep_paths": []})
    assert r.json()["bot"]["source"]["keep_paths"] == []
    r = ghc.put(f"/api/bots/{bot['id']}/source", json={"type": "github", "repo": "octo/bot", "branch": "main"})
    assert r.json()["bot"]["source"]["keep_paths"] == []  # omitted -> unchanged


def test_keep_glob_and_backup_pruning(tmp_path):
    rx = compile_keep(["data/**", "config.py", "*.db", "logs/", "**/secret.json", "cogs"])
    for yes in ["data/a.json", "data/x/y.json", "config.py", "x.db", "logs/a.txt", "secret.json", "a/b/secret.json",
                "cogs", "cogs/a.py"]:
        assert rx.match(yes), yes
    for no in ["main.py", "src/config.py", "sub/x.db", "logsx/a", "cogsx.py", "datax/a"]:
        assert not rx.match(no), no
    root = tmp_path / "backups"
    for i in range(12):
        (root / f"20261115T1200{i:02d}Z").mkdir(parents=True)
    prune_backups(root)
    names = sorted(p.name for p in root.iterdir())
    assert len(names) == 10 and names[0] == "20261115T120002Z"


# B ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host,ok", [
    ("evil.example.com", False), ("attacker.com:8080", False), ("botdock.mydomain.net", False),
    ("192.168.1.5:8080", True), ("10.0.0.2", True), ("[::1]:8080", True), ("localhost:8080", True),
    ("nas", True), ("NAS:8080", True), ("nas.local", True), ("bots.lan", True), ("x.home", True),
    ("x.internal", True), ("box.home.arpa:8080", True),
])
def test_host_validation(client, host, ok):
    for method, path in (("get", "/api/health"), ("get", "/"), ("post", "/api/auth/setup")):
        r = getattr(client, method)(path, headers={"Host": host}, **({"json": {}} if method == "post" else {}))
        if ok:
            assert r.status_code != 403, (host, path, r.text)
        else:
            assert r.status_code == 403, (host, path)
            # API callers get JSON; a browser opening the UI gets a readable page.
            body = r.json()["detail"] if path.startswith("/api/") else r.text
            assert "BOTDOCK_ALLOWED_HOSTS" in body, (host, path)


def test_allowed_hosts_setting(make_client):
    c = make_client(allowed_hosts=("bots.example.com",))
    assert c.get("/api/health", headers={"Host": "bots.example.com:443"}).status_code == 200
    assert c.get("/api/health", headers={"Host": "other.example.com"}).status_code == 403
    c2 = make_client(allowed_hosts=("*",))
    assert c2.get("/api/health", headers={"Host": "anything.example.com"}).status_code == 200


def test_origin_must_match_host(client):
    body = setup_body(client, PASSWORD)
    r = client.post("/api/auth/setup", json=body, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403 and "Origin" in r.json()["detail"]
    assert client.post("/api/auth/setup", json=body, headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/auth/setup", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/bots", headers={"Origin": "http://evil.example.com"}).status_code == 200  # GET: not checked
    r = client.post("/api/auth/logout", headers={"Origin": "http://testserver:80"})
    assert r.status_code == 200


def test_setup_code_flow(client, data_dir):
    ctx = client.app.state.ctx
    code_file = data_dir / "setup-code.txt"
    assert client.get("/api/auth/state").json()["setup_code_required"] is True
    assert code_file.read_text().strip() == ctx.setup_code
    if os.name != "nt":
        assert (code_file.stat().st_mode & 0o777) == 0o600
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 403 and "setup code" in r.json()["detail"]
    for _ in range(4):
        assert client.post("/api/auth/setup", json={"password": PASSWORD, "setup_code": "AAAA-BBBB-CCCC"}).status_code == 403
    r = client.post("/api/auth/setup", json={"password": PASSWORD, "setup_code": ctx.setup_code})
    assert r.status_code == 429  # rate limited like login, even with the right code
    ctx.auth.limiter.reset("testclient")
    code = ctx.setup_code.lower().replace("-", " ")  # forgiving about case/separators
    assert client.post("/api/auth/setup", json={"password": PASSWORD, "setup_code": code}).status_code == 200
    assert not code_file.exists()
    st = client.get("/api/auth/state").json()
    assert st == {"setup_required": False, "authenticated": True, "setup_code_required": False}


@pytest.mark.parametrize("weak", ["changeme", "change-me-please", "password", "Password", "abc"])
def test_weak_seed_password_refused(make_client, data_dir, weak, caplog):
    c = make_client(password=weak)
    st = c.get("/api/auth/state").json()
    assert st["setup_required"] is True and st["setup_code_required"] is True
    assert (data_dir / "setup-code.txt").exists()
    assert "NOT using the password from BOTDOCK_PASSWORD" in caplog.text


def test_password_file(make_client, data_dir, tmp_path):
    pw_file = tmp_path / "seed-pw"
    pw_file.write_text("a-strong-seed-pw\n")
    c = make_client(password_file=str(pw_file))
    assert not pw_file.exists()  # read once, then deleted
    st = c.get("/api/auth/state").json()
    assert st["setup_required"] is False and st["setup_code_required"] is False
    assert not (data_dir / "setup-code.txt").exists()
    assert c.post("/api/auth/login", json={"password": "a-strong-seed-pw"}).status_code == 200


def test_weak_password_file_falls_back(make_client, data_dir, tmp_path):
    pw_file = tmp_path / "seed-pw"
    pw_file.write_text("changeme")
    c = make_client(password_file=str(pw_file))
    assert not pw_file.exists()
    assert c.get("/api/auth/state").json()["setup_code_required"] is True


# D ------------------------------------------------------------------------------------------------


def test_body_caps_on_received_bytes(client):
    def chunks(n, size=64 * 1024):
        for _ in range(n):
            yield b"x" * size

    # chunked (no Content-Length): the cap is enforced on what arrives
    r = client.post("/api/auth/login", content=chunks(4), headers={"content-type": "application/json"})
    assert r.status_code == 413
    r = client.post("/api/auth/login", content=b"{" + b" " * 70_000 + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413
    # small bodies are fine
    assert client.post("/api/auth/login", json={"password": "x" * 100}).status_code in (401, 409)


def test_unauthenticated_upload_rejected_before_body(client, data_dir):
    client.post("/api/auth/setup", json=setup_body(client, PASSWORD))
    client.cookies.clear()
    consumed = []

    def body():
        yield b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.py\"\r\n\r\n"
        for i in range(50):
            consumed.append(i)
            yield b"x" * 65536

    r = client.post("/api/bots", content=body(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 401
    tmp = data_dir / "tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []


# E ------------------------------------------------------------------------------------------------


def test_shutdown_uses_short_grace(make_client):
    c = make_client(stop_timeout=10.0, shutdown_stop_timeout=2.0)
    c.post("/api/auth/setup", json=setup_body(c, PASSWORD))
    stubborn = (b"import signal, time\n"
                b"if hasattr(signal, 'SIGTERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                b"print('ignoring SIGTERM', flush=True)\nwhile True: time.sleep(0.1)\n")
    bot_id = create_bot(c, "Stubborn", [("s.py", stubborn)])["bot"]["id"]
    c.post(f"/api/bots/{bot_id}/start")
    pid = wait_state(c, bot_id, "running")["runtime"]["pid"]
    wait_for(lambda: "ignoring SIGTERM" in log_texts(c, bot_id))
    t = time.monotonic()
    c.__exit__(None, None, None)
    took = time.monotonic() - t
    assert took < 5.0, f"shutdown took {took:.1f}s"
    import psutil

    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_default_shutdown_budget():
    from botdock.config import Settings

    s = Settings()
    assert s.shutdown_stop_timeout <= 5.0 and s.stop_timeout == 10.0
    src = (Path(__file__).resolve().parents[1] / "botdock" / "__main__.py").read_text()
    assert "timeout_graceful_shutdown=2" in src  # 2 s HTTP drain + 5 s bot grace + kill < Docker's 10 s


# F ------------------------------------------------------------------------------------------------


def test_stream_subscribers_cleaned_up(authed):
    ctx = authed.app.state.ctx
    bot_id = create_bot(authed, "S", [("main.py", b"")])["bot"]["id"]
    rt = ctx.supervisor.runtime(bot_id)
    for url in (f"/api/bots/{bot_id}/logs/stream", "/api/events"):
        with authed.stream("GET", url) as r:
            assert r.status_code == 200
            next(r.iter_lines())
    wait_for(lambda: not rt.subscribers and not ctx.events.subscribers and not ctx.streams, desc="cleanup")


# G ------------------------------------------------------------------------------------------------


def test_upload_reports_skipped_env_keys(authed):
    res = create_bot(authed, "Env", [("b/main.py", b""), ("b/.env", b"A=1\nB=2\nBOTDOCK_X=3\n1BAD=4\n")])
    assert sorted(res["imported_env_keys"]) == ["A", "B"]
    assert sorted(res["skipped_env_keys"]) == ["1BAD", "BOTDOCK_X"]
    bot_id = res["bot"]["id"]
    r = upload(authed, f"/api/bots/{bot_id}/code", [(".env", b"A=changed\nC=3\n")])
    assert r.json()["imported_env_keys"] == ["C"] and r.json()["skipped_env_keys"] == ["A"]
    assert authed.get(f"/api/bots/{bot_id}/env/A").json()["value"] == "1"
    r = upload(authed, f"/api/bots/{bot_id}/code", [("x.py", b"")])
    assert r.json()["skipped_env_keys"] == []
