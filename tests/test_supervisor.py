"""Real subprocess tests of the supervisor (bots run on the current interpreter, no venv)."""

from __future__ import annotations

import json
import sqlite3
import threading

import psutil
import pytest

from conftest import setup_body, create_bot, get_bot, log_texts, upload, wait_for, wait_state

TICKER = b"""
import os, sys, time
print("hello", os.getenv("GREETING", "nobody"), flush=True)
i = 0
while True:
    i += 1
    print(f"tick {i}", flush=True)
    time.sleep(0.1)
"""

SPAWNER = b"""
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
print("child", p.pid, flush=True)
time.sleep(600)
"""

CRASH = b"""
import sys
print("crashing", flush=True)
sys.exit(3)
"""

TAIL = b"""
import sys
for i in range(2000):
    print(f"line {i}")
sys.stderr.write("last words without newline")
sys.exit(1)
"""

ENVDUMP = b"""
import json, os
print("ENV " + json.dumps(dict(os.environ)))
"""


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def start(client, bot_id, expect=200):
    r = client.post(f"/api/bots/{bot_id}/start")
    assert r.status_code == expect, r.text
    return r.json()


def test_start_logs_stop(authed):
    bot_id = create_bot(authed, "Ticker", [("ticker.py", TICKER)])["bot"]["id"]
    authed.put(f"/api/bots/{bot_id}/env/GREETING", json={"value": "world"})
    body = start(authed, bot_id)
    assert body["bot"]["runtime"]["state"] in ("installing", "starting", "running")
    bot = wait_state(authed, bot_id, "running")
    rt = bot["runtime"]
    assert rt["pid"] and rt["started_at"].endswith("Z") and rt["last_error"] is None
    wait_for(lambda: any(t.startswith("tick 3") for t in log_texts(authed, bot_id)), desc="ticks")
    lines = authed.get(f"/api/bots/{bot_id}/logs?tail=5000").json()["lines"]
    assert any(line["text"] == "hello world" and line["stream"] == "stdout" for line in lines)
    assert any(line["stream"] == "system" and line["text"].startswith("Starting") for line in lines)
    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(line["ts"].endswith("Z") for line in lines)
    assert len(authed.get(f"/api/bots/{bot_id}/logs?tail=2").json()["lines"]) == 2
    # stats show up while running
    wait_for(lambda: get_bot(authed, bot_id)["runtime"]["memory_bytes"], desc="memory stats")

    pid = rt["pid"]
    r = authed.post(f"/api/bots/{bot_id}/stop")
    assert r.status_code == 200
    assert r.json()["bot"]["runtime"]["state"] == "stopped"
    assert r.json()["bot"]["runtime"]["pid"] is None
    wait_for(lambda: not alive(pid), desc="process gone")
    n = len(log_texts(authed, bot_id))
    dl = authed.get(f"/api/bots/{bot_id}/logs/download")
    assert dl.status_code == 200 and "hello world" in dl.text and "attachment" in dl.headers["content-disposition"]
    assert authed.delete(f"/api/bots/{bot_id}/logs").json() == {"ok": True}
    assert log_texts(authed, bot_id) == [] and n > 0
    assert "hello world" not in authed.get(f"/api/bots/{bot_id}/logs/download").text


def test_stop_kills_whole_tree(authed):
    bot_id = create_bot(authed, "Spawner", [("spawn.py", SPAWNER)])["bot"]["id"]
    start(authed, bot_id)
    bot = wait_state(authed, bot_id, "running")
    texts = wait_for(lambda: [t for t in log_texts(authed, bot_id) if t.startswith("child ")], desc="child pid")
    child_pid = int(texts[0].split()[1])
    root_pid = bot["runtime"]["pid"]
    assert alive(child_pid) and alive(root_pid)
    authed.post(f"/api/bots/{bot_id}/stop")
    wait_for(lambda: not alive(child_pid) and not alive(root_pid), timeout=10, desc="tree killed")


def test_output_tail_not_lost(authed):
    bot_id = create_bot(authed, "Tail", [("tail.py", TAIL)])["bot"]["id"]
    authed.patch(f"/api/bots/{bot_id}", json={"restart_policy": "never"})
    start(authed, bot_id)
    bot = wait_state(authed, bot_id, "crashed")
    assert bot["runtime"]["exit_code"] == 1 and bot["runtime"]["restart_count"] == 0
    lines = authed.get(f"/api/bots/{bot_id}/logs?tail=5000").json()["lines"]
    texts = [line["text"] for line in lines]
    assert "line 1999" in texts
    assert sum(1 for t in texts if t.startswith("line ")) == 2000
    last = next(line for line in lines if line["text"] == "last words without newline")
    exit_line = next(line for line in lines if line["text"] == "Process exited with code 1")
    assert last["stream"] == "stderr" and last["seq"] < exit_line["seq"]
    assert not any(t.startswith("Restarting") for t in texts)


def test_crash_backoff_then_give_up(authed):
    bot_id = create_bot(authed, "Crashy", [("crash.py", CRASH)])["bot"]["id"]
    start(authed, bot_id)
    # waiting for a backoff restart: crashed + next_restart_at
    wait_for(lambda: get_bot(authed, bot_id)["runtime"]["next_restart_at"], desc="backoff scheduled")
    bot = wait_for(lambda: (b := get_bot(authed, bot_id))["runtime"]["state"] == "crashed"
                   and b["runtime"]["next_restart_at"] is None and b, timeout=20, desc="gave up")
    rt = bot["runtime"]
    assert rt["restart_count"] == 3  # max_rapid_failures=4 -> 3 automatic restarts
    assert "4 times" in rt["last_error"] and rt["exit_code"] == 3
    texts = log_texts(authed, bot_id)
    assert texts.count("crashing") == 4
    assert [t for t in texts if t.startswith("Restarting in")] == [
        "Restarting in 0.2s", "Restarting in 0.4s", "Restarting in 0.8s"]


def test_restart_policy_never_and_always(authed):
    bot_id = create_bot(authed, "Never", [("crash.py", CRASH)])["bot"]["id"]
    authed.patch(f"/api/bots/{bot_id}", json={"restart_policy": "never"})
    start(authed, bot_id)
    bot = wait_state(authed, bot_id, "crashed")
    assert bot["runtime"]["restart_count"] == 0 and bot["runtime"]["next_restart_at"] is None
    assert bot["runtime"]["last_error"] == "Process exited with code 3"

    ok_id = create_bot(authed, "CleanExit", [("ok.py", b"print('done')\n")])["bot"]["id"]
    start(authed, ok_id)
    bot = wait_state(authed, ok_id, "stopped")  # on-failure + exit 0 -> no restart
    assert bot["runtime"]["exit_code"] == 0 and bot["runtime"]["restart_count"] == 0

    authed.patch(f"/api/bots/{ok_id}", json={"restart_policy": "always"})
    start(authed, ok_id)
    wait_for(lambda: get_bot(authed, ok_id)["runtime"]["restart_count"] >= 2, desc="always restarts")
    authed.post(f"/api/bots/{ok_id}/stop")
    assert get_bot(authed, ok_id)["runtime"]["state"] == "stopped"


def test_stop_during_backoff(make_client):
    c = make_client(backoff_base=30.0, backoff_max=60.0)
    c.post("/api/auth/setup", json=setup_body(c, "password123"))
    bot_id = create_bot(c, "Slow", [("crash.py", CRASH)])["bot"]["id"]
    start(c, bot_id)
    wait_for(lambda: get_bot(c, bot_id)["runtime"]["next_restart_at"], desc="backoff")
    r = c.post(f"/api/bots/{bot_id}/stop").json()["bot"]["runtime"]
    assert r["state"] == "stopped" and r["next_restart_at"] is None
    # manual start during backoff starts immediately instead
    start(c, bot_id)
    wait_for(lambda: log_texts(c, bot_id).count("crashing") >= 2, desc="second run")


def test_needs_restart_flag(authed):
    bot_id = create_bot(authed, "Flag", [("p/main.py", TICKER), ("p/x.txt", b"")])["bot"]["id"]
    authed.put(f"/api/bots/{bot_id}/env/A", json={"value": "1"})
    assert get_bot(authed, bot_id)["needs_restart"] is False  # not running -> no flag
    start(authed, bot_id)
    wait_state(authed, bot_id, "running")
    assert get_bot(authed, bot_id)["needs_restart"] is False
    authed.put(f"/api/bots/{bot_id}/env/A", json={"value": "2"})
    assert get_bot(authed, bot_id)["needs_restart"] is True
    pid = get_bot(authed, bot_id)["runtime"]["pid"]
    r = authed.post(f"/api/bots/{bot_id}/restart")
    assert r.status_code == 200
    bot = wait_state(authed, bot_id, "running")
    assert bot["needs_restart"] is False and bot["runtime"]["pid"] != pid
    assert not alive(pid)
    # code upload, file edit and settings changes also flag it
    upload(authed, f"/api/bots/{bot_id}/code", [("p/x.txt", b"changed")])
    assert get_bot(authed, bot_id)["needs_restart"] is True
    authed.post(f"/api/bots/{bot_id}/restart")
    wait_state(authed, bot_id, "running")
    authed.put(f"/api/bots/{bot_id}/files/content", json={"path": "x.txt", "content": "y"})
    assert get_bot(authed, bot_id)["needs_restart"] is True
    authed.post(f"/api/bots/{bot_id}/restart")
    wait_state(authed, bot_id, "running")
    authed.patch(f"/api/bots/{bot_id}", json={"description": "cosmetic"})
    assert get_bot(authed, bot_id)["needs_restart"] is False
    authed.patch(f"/api/bots/{bot_id}", json={"args": "--flag"})
    assert get_bot(authed, bot_id)["needs_restart"] is True
    authed.post(f"/api/bots/{bot_id}/stop")


def test_no_entrypoint_conflict(authed):
    bot_id = create_bot(authed, "NoEntry", [("x/one.py", b""), ("x/two.py", b"")])["bot"]["id"]
    r = authed.post(f"/api/bots/{bot_id}/start")
    assert r.status_code == 409 and r.json()["detail"] == "No entrypoint set"
    assert authed.post(f"/api/bots/{bot_id}/restart").status_code == 409
    assert authed.post("/api/bots/b_00000000/start").status_code == 404


def test_concurrent_starts_spawn_one_process(authed):
    bot_id = create_bot(authed, "Double", [("ticker.py", TICKER)])["bot"]["id"]
    results = []

    def hit(path):
        results.append(authed.post(f"/api/bots/{bot_id}/{path}").status_code)

    threads = [threading.Thread(target=hit, args=(p,)) for p in ["start", "start", "restart", "start", "start"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 5
    bot = wait_state(authed, bot_id, "running")
    pid = bot["runtime"]["pid"]
    texts = log_texts(authed, bot_id)
    assert texts.count("hello nobody") >= 1
    # at most one live bot process: every earlier process must be gone
    live = [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and "ticker.py" in p.info["cmdline"]
            and alive(p.pid)]
    roots = {p.pid for p in live if p.pid == pid or (p.parent() and p.parent().pid == pid)}
    assert {p.pid for p in live} <= roots, "a second ticker process is running"
    authed.post(f"/api/bots/{bot_id}/stop")


def test_bot_env_is_clean(authed, monkeypatch):
    monkeypatch.setenv("BOTDOCK_LEAK_CHECK", "should-not-leak")
    monkeypatch.setenv("SOME_HOST_SECRET", "host-only")
    bot_id = create_bot(authed, "EnvDump", [("dump.py", ENVDUMP)])["bot"]["id"]
    authed.put(f"/api/bots/{bot_id}/env/DISCORD_TOKEN", json={"value": "tok-123"})
    start(authed, bot_id)
    wait_state(authed, bot_id, "stopped")
    line = next(t for t in log_texts(authed, bot_id) if t.startswith("ENV "))
    env = json.loads(line[4:])
    assert env["DISCORD_TOKEN"] == "tok-123"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "PATH" in env and "HOME" in env
    assert not any(k.upper().startswith("BOTDOCK_") for k in env)
    assert "SOME_HOST_SECRET" not in env
    # secrets never reach the log files except through the bot's own output
    assert "tok-123" not in "".join(t for t in log_texts(authed, bot_id) if not t.startswith("ENV "))


def test_cwd_is_src_and_writable(authed, data_dir):
    script = b"import os, json\nos.makedirs('data', exist_ok=True)\nopen('data/state.json','w').write(json.dumps({'cwd': os.getcwd()}))\nprint('wrote')\n"
    bot_id = create_bot(authed, "Writer", [("main.py", script)])["bot"]["id"]
    start(authed, bot_id)
    wait_state(authed, bot_id, "stopped")
    state = data_dir / "bots" / bot_id / "src" / "data" / "state.json"
    assert state.exists()
    assert json.loads(state.read_text())["cwd"].lower() == str(state.parent.parent).lower()


def test_autostart_on_boot_and_shutdown_keeps_desired_state(make_client, data_dir):
    c1 = make_client()
    c1.post("/api/auth/setup", json=setup_body(c1, "password123"))
    run_id = create_bot(c1, "Runner", [("ticker.py", TICKER)])["bot"]["id"]
    stopped_id = create_bot(c1, "Stopped", [("ticker.py", TICKER)])["bot"]["id"]
    noauto_id = create_bot(c1, "NoAuto", [("ticker.py", TICKER)])["bot"]["id"]
    c1.patch(f"/api/bots/{noauto_id}", json={"autostart": False})
    for b in (run_id, stopped_id, noauto_id):
        start(c1, b)
    for b in (run_id, stopped_id, noauto_id):
        wait_state(c1, b, "running")
    c1.post(f"/api/bots/{stopped_id}/stop")
    pids = [get_bot(c1, b)["runtime"]["pid"] for b in (run_id, noauto_id)]
    c1.__exit__(None, None, None)  # BotDock shutdown
    for pid in pids:
        assert not alive(pid), "shutdown must stop every bot"
    db = sqlite3.connect(str(data_dir / "botdock.db"))
    desired = dict(db.execute("SELECT id, desired_state FROM bots").fetchall())
    db.close()
    assert desired == {run_id: "running", stopped_id: "stopped", noauto_id: "running"}

    c2 = make_client()
    c2.post("/api/auth/login", json={"password": "password123"})
    wait_state(c2, run_id, "running")
    assert get_bot(c2, stopped_id)["runtime"]["state"] == "stopped"
    assert get_bot(c2, noauto_id)["runtime"]["state"] == "stopped"
    # logs from before the restart are served from the log file
    assert "hello nobody" in log_texts(c2, stopped_id)


def test_delete_running_bot(authed, data_dir):
    bot_id = create_bot(authed, "Doomed", [("ticker.py", TICKER)])["bot"]["id"]
    start(authed, bot_id)
    pid = wait_state(authed, bot_id, "running")["runtime"]["pid"]
    assert authed.delete(f"/api/bots/{bot_id}").json() == {"ok": True}
    assert not alive(pid)
    assert not (data_dir / "bots" / bot_id).exists()


def test_reinstall_without_venv(authed):
    bot_id = create_bot(authed, "Re", [("ticker.py", TICKER)])["bot"]["id"]
    start(authed, bot_id)
    wait_state(authed, bot_id, "running")
    r = authed.post(f"/api/bots/{bot_id}/reinstall")
    assert r.status_code == 200
    wait_state(authed, bot_id, "running")
    authed.post(f"/api/bots/{bot_id}/stop")


def test_log_sse_stream(authed):
    bot_id = create_bot(authed, "Streamer", [("ticker.py", TICKER)])["bot"]["id"]
    start(authed, bot_id)
    wait_for(lambda: any(t.startswith("tick 2") for t in log_texts(authed, bot_id)), desc="ticks")
    first_seq = authed.get(f"/api/bots/{bot_id}/logs?tail=1").json()["lines"][0]["seq"]
    events = []
    with authed.stream("GET", f"/api/bots/{bot_id}/logs/stream?after={first_seq}") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for raw in r.iter_lines():
            if raw.startswith("data: "):
                events.append(json.loads(raw[6:]))
                if len(events) >= 5:
                    break
    assert all(e["seq"] > first_seq for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert any(e["text"].startswith("tick") for e in events)

    seen = []
    with authed.stream("GET", "/api/events") as r:
        for raw in r.iter_lines():
            if raw == "event: bot":
                seen.append(raw)
            elif raw.startswith("data: ") and seen:
                data = json.loads(raw[6:])
                assert data["id"] == bot_id and data["runtime"]["state"] == "running"
                break
    authed.post(f"/api/bots/{bot_id}/stop")
