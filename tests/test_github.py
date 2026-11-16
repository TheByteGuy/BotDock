"""GitHub integration against an in-process fake GitHub (no network)."""

from __future__ import annotations

import json
import sqlite3
import tarfile
import io
import time

import httpx
import pytest

from conftest import setup_body, PASSWORD, create_bot, get_bot, log_texts, wait_for, wait_state
from fake_github import FakeGitHub

TICKER = """
import time
print("version", open("version.txt").read().strip(), flush=True)
while True:
    time.sleep(0.1)
"""


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def ghc(make_client, gh):
    """Authenticated BotDock client wired to the fake GitHub (not yet connected)."""
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app))
    assert c.post("/api/auth/setup", json=setup_body(c, PASSWORD)).status_code == 200
    return c


def connect(c):
    r = c.post("/api/github/token", json={"token": "good-token"})
    assert r.status_code == 200, r.text
    return r.json()


def ctx(c):
    return c.app.state.ctx


def poll_once(c):
    c.portal.call(ctx(c).github.poll_once)


def src_file(data_dir, bot_id, rel):
    return data_dir / "bots" / bot_id / "src" / rel


def make_bot_from_repo(c, repo="octo/bot", **body):
    r = c.post("/api/bots/github", json={"name": body.pop("name", "Repo Bot"), "repo": repo, **body})
    assert r.status_code == 201, r.text
    return r.json()


def sync_and_wait(c, bot_id):
    r = c.post(f"/api/bots/{bot_id}/source/sync")
    assert r.status_code == 200, r.text
    return wait_for(lambda: (b := get_bot(c, bot_id))["source"]["syncing"] is False and b, desc="sync done")


# --- connection -------------------------------------------------------------------------------


def test_not_connected_errors(ghc, gh):
    st = ghc.get("/api/github").json()
    assert st["connected"] is False and st["login"] is None and st["poll_interval_seconds"] == 120
    for method, path, body in [
        ("get", "/api/github/repos", None),
        ("get", "/api/github/repos/octo/x/branches", None),
        ("post", "/api/bots/github", {"name": "x", "repo": "octo/x"}),
    ]:
        r = getattr(ghc, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 409 and r.json() == {"detail": "GitHub is not connected"}, (path, r.text)
    r = ghc.post("/api/github/device/start")
    assert r.status_code == 409
    assert ghc.post("/api/github/device/poll").json() == {"status": "none"}
    # an upload bot has nothing to check
    bot_id = create_bot(ghc, "Up", [("main.py", b"")])["bot"]["id"]
    assert get_bot(ghc, bot_id)["source"] == {"type": "upload"}
    assert ghc.post(f"/api/bots/{bot_id}/source/check").status_code == 409


def test_token_connect_reject_disconnect(ghc, gh, data_dir):
    r = ghc.post("/api/github/token", json={"token": "bad-token"})
    assert r.status_code == 400 and "bad-token" not in r.text
    st = connect(ghc)
    assert st["connected"] is True and st["login"] == "octo" and st["name"] == "Octo Cat"
    assert st["method"] == "token" and st["avatar_url"].startswith("https://avatars.githubusercontent.com")
    assert st["rate_limit_remaining"] == 4999
    for path in ("/api/github", "/api/bots"):
        assert "good-token" not in ghc.get(path).text
    raw = b"".join(f.read_bytes() for f in data_dir.glob("botdock.db*"))
    assert b"good-token" not in raw
    assert ghc.delete("/api/github").json() == {"ok": True}
    assert ghc.get("/api/github").json()["connected"] is False


def test_device_flow_states(ghc, gh):
    assert ghc.put("/api/github/settings", json={"client_id": "Iv1.testclient"}).json()["client_id"] == "Iv1.testclient"
    assert ghc.put("/api/github/settings", json={"poll_interval_seconds": 10}).status_code == 400
    assert ghc.put("/api/github/settings", json={"poll_interval_seconds": 300}).json()["poll_interval_seconds"] == 300

    gh.device_script = ["pending", "slow_down", "ok"]
    r = ghc.post("/api/github/device/start")
    assert r.status_code == 200
    body = r.json()
    assert body["user_code"] == "WDJB-MJHT" and body["verification_uri"].endswith("/login/device")
    assert "device_code" not in body and body["expires_in"] == 900
    assert ghc.post("/api/github/device/poll").json() == {"status": "pending"}
    assert ghc.post("/api/github/device/poll").json() == {"status": "pending"}  # slow_down
    assert ghc.post("/api/github/device/poll").json() == {"status": "connected"}
    st = ghc.get("/api/github").json()
    assert st["connected"] and st["method"] == "device" and st["login"] == "octo"
    assert ghc.post("/api/github/device/poll").json() == {"status": "none"}

    for step, expected in (("expired", "expired"), ("denied", "denied")):
        gh.device_script = [step]
        ghc.post("/api/github/device/start")
        assert ghc.post("/api/github/device/poll").json() == {"status": expected}
        assert ghc.post("/api/github/device/poll").json() == {"status": "none"}


def test_device_flow_respects_interval_and_expiry(ghc, gh):
    ghc.put("/api/github/settings", json={"client_id": "Iv1.testclient"})
    gh.device_interval = 5
    gh.device_script = ["pending"]
    ghc.post("/api/github/device/start")
    for _ in range(3):
        assert ghc.post("/api/github/device/poll").json() == {"status": "pending"}
    assert gh.device_polls == 0  # BotDock waits out GitHub's interval itself
    ctx(ghc).github._device["expires_at"] = time.time() - 1
    assert ghc.post("/api/github/device/poll").json() == {"status": "expired"}
    ghc.put("/api/github/settings", json={"client_id": "Iv1.unknown"})
    assert ghc.post("/api/github/device/start").status_code == 502


# --- repos ------------------------------------------------------------------------------------


def test_repo_listing_filter_pagination_and_branches(ghc, gh):
    for i in range(150):
        gh.add_repo(f"octo/repo{i:03d}", {"main.py": ""}, private=i % 2 == 0, pushed_at=f"2026-10-01T00:{i % 60:02d}:{i // 60:02d}Z")
    gh.add_repo("octo/ticketbot", {"main.py": ""}, pushed_at="2026-11-14T00:00:00Z", extra_branches=("dev", "feature"))
    connect(ghc)
    p1 = ghc.get("/api/github/repos?page=1").json()
    assert len(p1["repos"]) == 30 and p1["has_more"] is True
    assert p1["repos"][0]["full_name"] == "octo/ticketbot"  # most recently pushed first
    first = p1["repos"][0]
    assert set(first) == {"full_name", "name", "owner", "private", "default_branch", "description", "language",
                          "pushed_at", "html_url"}
    assert first["owner"] == "octo" and first["name"] == "ticketbot"
    p6 = ghc.get("/api/github/repos?page=6").json()
    assert len(p6["repos"]) == 1 and p6["has_more"] is False  # 151 repos fetched across 2 upstream pages
    q = ghc.get("/api/github/repos?q=TICKET").json()
    assert [r["full_name"] for r in q["repos"]] == ["octo/ticketbot"] and q["has_more"] is False
    assert len(ghc.get("/api/github/repos?q=repo1").json()["repos"]) == 30
    b = ghc.get("/api/github/repos/octo/ticketbot/branches").json()
    assert b == {"branches": ["main", "dev", "feature"], "default_branch": "main"}
    assert ghc.get("/api/github/repos/octo/nope/branches").status_code == 404
    assert ghc.get("/api/github/repos/octo/..%2F..%2Fx/branches").status_code in (400, 404)


# --- create from repo -------------------------------------------------------------------------


def test_create_bot_from_repo(ghc, gh, data_dir):
    sha = gh.add_repo("octo/bot", {
        "main.py": "print(1)\n", "data/tickets.json": "{}", ".env": "SECRET=1\n",
        "__pycache__/x.pyc": b"\0", "cogs/a.py": "a",
    }, private=True)
    connect(ghc)
    res = make_bot_from_repo(ghc, auto_update=False)
    bot = res["bot"]
    assert bot["entrypoint"] == "main.py" and res["imported_env_keys"] == [] and res["detected_entrypoints"] == ["main.py"]
    s = bot["source"]
    assert s["type"] == "github" and s["repo"] == "octo/bot" and s["branch"] == "main" and s["subdir"] == ""
    assert s["auto_update"] is False and s["commit_sha"] == sha and s["latest_sha"] == sha
    assert s["commit_message"] == "initial commit" and s["update_available"] is False
    assert s["commit_date"].endswith("Z") and s["last_synced_at"].endswith("Z") and s["last_error"] is None
    files = {e["path"] for e in ghc.get(f"/api/bots/{bot['id']}/files").json()["entries"] if e["type"] == "file"}
    assert files == {"main.py", "data/tickets.json", "cogs/a.py"}  # .env ignored, caches skipped
    assert bot["env_count"] == 0
    # token sent to the API host, never to codeload
    api_calls = [e for e in gh.log if e["path"].startswith("/repos/")]
    assert api_calls and all(e["auth"] == "Bearer good-token" for e in api_calls)
    codeload = [e for e in gh.log if "legacy.tar.gz" in e["path"]]
    assert codeload and all(e["auth"] is None and e["status"] == 200 for e in codeload)

    assert ghc.post("/api/bots/github", json={"name": "Repo Bot", "repo": "octo/bot"}).status_code == 409
    assert ghc.post("/api/bots/github", json={"name": "X", "repo": "octo/missing"}).status_code == 404
    assert ghc.post("/api/bots/github", json={"name": "X", "repo": "not a repo"}).status_code == 400
    assert ghc.post("/api/bots/github", json={"name": "X", "repo": "octo/bot", "branch": "nope"}).status_code == 404
    assert ghc.post("/api/bots/github", json={"name": "X", "repo": "octo/bot", "subdir": "../x"}).status_code == 400
    assert len(ghc.get("/api/bots").json()["bots"]) == 1


def test_create_with_subdir_and_branch(ghc, gh):
    gh.add_repo("octo/mono", {"README.md": "r", "bots/ticket/bot.py": "x", "bots/ticket/cogs/c.py": "c"},
                extra_branches=("dev",))
    gh.push("octo/mono", {"bots/ticket/dev_only.py": "d"}, branch="dev")
    connect(ghc)
    bot = make_bot_from_repo(ghc, repo="octo/mono", branch="dev", subdir="bots/ticket")["bot"]
    files = {e["path"] for e in ghc.get(f"/api/bots/{bot['id']}/files").json()["entries"] if e["type"] == "file"}
    assert files == {"bot.py", "cogs/c.py", "dev_only.py"}
    assert bot["entrypoint"] == "bot.py" and bot["source"]["subdir"] == "bots/ticket"
    r = ghc.post("/api/bots/github", json={"name": "Y", "repo": "octo/mono", "subdir": "nope"})
    assert r.status_code == 400 and "Subfolder" in r.json()["detail"]


# --- sync rules -------------------------------------------------------------------------------


def test_sync_rules_preserve_runtime_data(ghc, gh, data_dir):
    gh.add_repo("octo/bot", {
        "main.py": "v1", "data/tickets.json": '{"tickets": []}', "old.py": "old",
        "gone_mod.py": "gone", "data/static.json": "S", "data/later.json": "upstream-later",
    })
    gh.push("octo/bot", delete=("data/later.json",))
    connect(ghc)
    bot_id = make_bot_from_repo(ghc, auto_update=False)["bot"]["id"]
    f = lambda rel: src_file(data_dir, bot_id, rel)  # noqa: E731

    # the bot rewrites its committed data file, modifies another, creates its own files
    f("data/tickets.json").write_text('{"tickets": [1, 2, 3]}')
    f("gone_mod.py").write_text("locally edited")
    f("data/runtime_only.json").write_text("bot-made")
    f("data/later.json").write_text("bot-made-later")

    # upstream: code change, two deletions, tickets.json untouched
    sha2 = gh.push("octo/bot", {"main.py": "v2"}, delete=("old.py", "gone_mod.py"), message="v2")
    ghc.post(f"/api/bots/{bot_id}/source/check")
    assert get_bot(ghc, bot_id)["source"]["update_available"] is True
    bot = sync_and_wait(ghc, bot_id)
    assert bot["source"]["commit_sha"] == sha2 and bot["source"]["update_available"] is False
    assert bot["source"]["last_error"] is None and bot["source"]["commit_message"] == "v2"
    assert f("main.py").read_text() == "v2"
    assert f("data/tickets.json").read_text() == '{"tickets": [1, 2, 3]}'  # runtime data preserved
    assert not f("old.py").exists()  # unmodified + removed upstream -> deleted
    assert f("gone_mod.py").read_text() == "locally edited"  # modified + removed upstream -> kept
    assert f("data/runtime_only.json").read_text() == "bot-made"  # never synced -> untouched
    assert f("data/static.json").read_text() == "S"
    texts = log_texts(ghc, bot_id)
    assert "kept locally modified gone_mod.py (removed upstream)" in texts

    # upstream changes the data file itself -> upstream wins, with a log line
    gh.push("octo/bot", {"data/tickets.json": '{"tickets": ["reset"]}', "data/later.json": "upstream-new"})
    sync_and_wait(ghc, bot_id)
    assert f("data/tickets.json").read_text() == '{"tickets": ["reset"]}'
    assert f("data/later.json").read_text() == "upstream-new"  # bot-made file later added upstream
    texts = log_texts(ghc, bot_id)
    assert "overwrote locally modified data/tickets.json" in texts
    assert "overwrote locally modified data/later.json" in texts

    # a sync with nothing new upstream changes nothing locally
    f("data/tickets.json").write_text("runtime again")
    sync_and_wait(ghc, bot_id)
    assert f("data/tickets.json").read_text() == "runtime again"

    # entrypoint re-detected only if the current one disappears
    gh.push("octo/bot", {"bot.py": "b"}, delete=("main.py",))
    bot = sync_and_wait(ghc, bot_id)
    assert bot["entrypoint"] == "bot.py"


@pytest.mark.parametrize("attack", ["traversal", "absolute", "corrupt", "too_many", "too_big"])
def test_bad_tarball_leaves_src_untouched(make_client, gh, data_dir, attack):
    overrides = {"max_upload_files": 5} if attack == "too_many" else {"max_upload_bytes": 2000} if attack == "too_big" else {}
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app), **overrides)
    c.post("/api/auth/setup", json=setup_body(c, PASSWORD))
    gh.add_repo("octo/bot", {"main.py": "v1", "keep.txt": "k"})
    connect(c)
    bot_id = make_bot_from_repo(c, auto_update=False)["bot"]["id"]
    before = {p.name: p.read_bytes() for p in (data_dir / "bots" / bot_id / "src").iterdir()}
    gh.push("octo/bot", {"main.py": "v2"}, delete=("keep.txt",))

    def add_member(name):
        def hook(tf, top):
            ti = tarfile.TarInfo(name.replace("TOP", top))
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"evil"))
        return hook

    if attack == "traversal":
        gh.tar_hook = add_member("TOP/../../evil.py")
    elif attack == "absolute":
        gh.tar_hook = add_member("/tmp/evil.py")
    elif attack == "corrupt":
        gh.corrupt_tarball = True
    elif attack == "too_many":
        gh.push("octo/bot", {f"f{i}.py": "" for i in range(10)})
    elif attack == "too_big":
        gh.push("octo/bot", {"big.bin": "x" * 5000})
    bot = sync_and_wait(c, bot_id)
    assert bot["source"]["last_error"], attack
    after = {p.name: p.read_bytes() for p in (data_dir / "bots" / bot_id / "src").iterdir()}
    assert after == before, attack
    assert not (data_dir / "evil.py").exists() and not (data_dir.parent / "evil.py").exists()
    assert any(t.startswith("Sync failed") for t in log_texts(c, bot_id))
    assert list((data_dir / "tmp").iterdir()) == []


def test_links_and_devices_in_tarball_are_skipped(ghc, gh, data_dir):
    gh.add_repo("octo/bot", {"main.py": "v1"})

    def hook(tf, top):
        for name, kind, target in (("link", tarfile.SYMTYPE, "/etc/passwd"), ("hard", tarfile.LNKTYPE, f"{top}/main.py"),
                                   ("dev", tarfile.CHRTYPE, "")):
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.type = kind
            ti.linkname = target
            tf.addfile(ti)

    gh.tar_hook = hook
    connect(ghc)
    bot_id = make_bot_from_repo(ghc, auto_update=False)["bot"]["id"]
    files = {p.name for p in (data_dir / "bots" / bot_id / "src").iterdir()}
    assert files == {"main.py"}
    assert "skipped link/special file link" in log_texts(ghc, bot_id)


# --- link / unlink ----------------------------------------------------------------------------


def test_link_existing_upload_bot_and_unlink(ghc, gh, data_dir):
    sha = gh.add_repo("octo/bot", {"main.py": "from repo"}, extra_branches=("dev",))
    bot_id = create_bot(ghc, "Up", [("main.py", b"local")])["bot"]["id"]
    assert ghc.put(f"/api/bots/{bot_id}/source", json={"type": "github", "repo": "bad"}).status_code == 400
    assert ghc.put(f"/api/bots/{bot_id}/source", json={"type": "svn"}).status_code == 400
    # linking without a token is allowed when a branch is given (nothing is fetched)
    r = ghc.put(f"/api/bots/{bot_id}/source", json={"type": "github", "repo": "octo/bot", "branch": "dev",
                                                       "subdir": "", "auto_update": True})
    assert r.status_code == 200
    s = r.json()["bot"]["source"]
    assert s["repo"] == "octo/bot" and s["branch"] == "dev" and s["commit_sha"] is None
    assert src_file(data_dir, bot_id, "main.py").read_text() == "local"  # linking does not pull
    connect(ghc)
    r = ghc.put(f"/api/bots/{bot_id}/source", json={"type": "github", "repo": "octo/bot", "auto_update": False})
    assert r.json()["bot"]["source"]["branch"] == "main"  # default branch looked up
    bot = wait_for(lambda: (b := get_bot(ghc, bot_id))["source"]["latest_sha"] and b, desc="background check")
    assert bot["source"]["latest_sha"] == sha and bot["source"]["update_available"] is True
    bot = sync_and_wait(ghc, bot_id)
    assert src_file(data_dir, bot_id, "main.py").read_text() == "from repo"
    assert "overwrote locally modified main.py" in log_texts(ghc, bot_id)
    r = ghc.put(f"/api/bots/{bot_id}/source", json={"type": "upload"})
    assert r.json()["bot"]["source"] == {"type": "upload"}
    assert src_file(data_dir, bot_id, "main.py").exists()
    assert ghc.post(f"/api/bots/{bot_id}/source/sync").status_code == 409


# --- poller -----------------------------------------------------------------------------------


def test_poller_restarts_running_keeps_stopped_starts_crashed(ghc, gh, data_dir):
    gh.add_repo("octo/run", {"main.py": TICKER, "version.txt": "1"})
    gh.add_repo("octo/stopped", {"main.py": TICKER, "version.txt": "1"})
    gh.add_repo("octo/crash", {"main.py": "import sys; sys.exit(2)", "version.txt": "1"})
    gh.add_repo("octo/manual", {"main.py": TICKER, "version.txt": "1"})
    connect(ghc)
    run_id = make_bot_from_repo(ghc, "octo/run", name="Run")["bot"]["id"]
    stopped_id = make_bot_from_repo(ghc, "octo/stopped", name="Stopped")["bot"]["id"]
    crash_id = make_bot_from_repo(ghc, "octo/crash", name="Crash")["bot"]["id"]
    manual_id = make_bot_from_repo(ghc, "octo/manual", name="Manual", auto_update=False)["bot"]["id"]
    ghc.patch(f"/api/bots/{crash_id}", json={"restart_policy": "never"})
    for b in (run_id, stopped_id, crash_id):
        ghc.post(f"/api/bots/{b}/start")
    old_pid = wait_state(ghc, run_id, "running")["runtime"]["pid"]
    wait_state(ghc, stopped_id, "running")
    ghc.post(f"/api/bots/{stopped_id}/stop")
    wait_state(ghc, crash_id, "crashed")

    poll_once(ghc)  # nothing new: no syncs
    assert "version 1" in log_texts(ghc, run_id)

    for repo in ("octo/run", "octo/stopped", "octo/manual"):
        gh.push(repo, {"version.txt": "2"})
    gh.push("octo/crash", {"main.py": TICKER, "version.txt": "2"})
    poll_once(ghc)

    bot = wait_state(ghc, run_id, "running")
    assert bot["runtime"]["pid"] != old_pid and bot["source"]["update_available"] is False
    wait_for(lambda: "version 2" in log_texts(ghc, run_id), desc="restarted with new code")
    assert get_bot(ghc, stopped_id)["runtime"]["state"] == "stopped"
    assert src_file(data_dir, stopped_id, "version.txt").read_text() == "2"
    wait_for(lambda: "version 2" in log_texts(ghc, crash_id), desc="crashed bot started")
    assert get_bot(ghc, crash_id)["runtime"]["state"] in ("starting", "running")
    manual = get_bot(ghc, manual_id)
    assert manual["source"]["update_available"] is True
    assert src_file(data_dir, manual_id, "version.txt").read_text() == "1"
    assert get_bot(ghc, manual_id)["runtime"]["state"] == "stopped"
    for b in (run_id, crash_id):
        ghc.post(f"/api/bots/{b}/stop")


def test_poller_etag_and_rate_limit(ghc, gh):
    gh.add_repo("octo/bot", {"main.py": "v1"})
    connect(ghc)
    bot_id = make_bot_from_repo(ghc)["bot"]["id"]
    gh.log.clear()
    poll_once(ghc)
    commits = [e for e in gh.log if "/commits/" in e["path"]]
    assert len(commits) == 1 and commits[0]["status"] == 304 and commits[0]["if_none_match"]
    assert get_bot(ghc, bot_id)["source"]["last_checked_at"]

    gh.rate_limit_reset = time.time() + 600
    poll_once(ghc)
    err = get_bot(ghc, bot_id)["source"]["last_error"]
    assert err.startswith("Rate limited by GitHub until")
    assert ctx(ghc).github.rate_limited_until > time.time() + 500
    n = len(gh.log)
    poll_once(ghc)
    ghc.post(f"/api/bots/{bot_id}/source/check")
    assert len(gh.log) == n  # backing off: no requests at all
    assert sum(1 for t in log_texts(ghc, bot_id) if t.startswith("GitHub: Rate limited")) == 1

    gh.rate_limit_reset = None
    ctx(ghc).github.rate_limited_until = 0
    sha = gh.push("octo/bot", {"main.py": "v2"})
    poll_once(ghc)
    s = get_bot(ghc, bot_id)["source"]
    assert s["last_error"] is None and s["commit_sha"] == sha


def test_token_rejected_and_disconnect_reported_on_bots(ghc, gh):
    gh.add_repo("octo/bot", {"main.py": "v1"})
    connect(ghc)
    bot_id = make_bot_from_repo(ghc)["bot"]["id"]
    del gh.tokens["good-token"]  # token revoked on GitHub
    r = ghc.post(f"/api/bots/{bot_id}/source/check")
    assert r.status_code == 200
    assert r.json()["bot"]["source"]["last_error"] == "GitHub rejected the token (sign in again)"
    assert "good-token" not in r.text
    ghc.delete("/api/github")
    assert get_bot(ghc, bot_id)["source"]["last_error"] == "GitHub is not connected"
    assert ghc.post(f"/api/bots/{bot_id}/source/check").status_code == 409
    assert ghc.post(f"/api/bots/{bot_id}/source/sync").status_code == 409
    poll_once(ghc)  # must not raise
    gh.tokens["good-token"] = {"login": "octo", "name": None, "avatar_url": None}
    connect(ghc)
    assert get_bot(ghc, bot_id)["source"]["last_error"] is None


def test_failed_sync_not_retried_for_same_head(ghc, gh):
    gh.add_repo("octo/bot", {"main.py": "v1"})
    connect(ghc)
    bot_id = make_bot_from_repo(ghc)["bot"]["id"]
    gh.push("octo/bot", {"main.py": "v2"})
    gh.corrupt_tarball = True
    poll_once(ghc)
    assert get_bot(ghc, bot_id)["source"]["last_error"]
    n = len([e for e in gh.log if "legacy.tar.gz" in e["path"]])
    poll_once(ghc)
    assert len([e for e in gh.log if "legacy.tar.gz" in e["path"]]) == n
    gh.corrupt_tarball = False
    sha = gh.push("octo/bot", {"main.py": "v3"})
    poll_once(ghc)
    assert get_bot(ghc, bot_id)["source"]["commit_sha"] == sha


def test_github_token_never_reaches_bot(ghc, gh):
    gh.add_repo("octo/bot", {"main.py": "import json, os\nprint('ENV ' + json.dumps(dict(os.environ)))\n"})
    connect(ghc)
    bot_id = make_bot_from_repo(ghc)["bot"]["id"]
    ghc.patch(f"/api/bots/{bot_id}", json={"restart_policy": "never"})
    ghc.post(f"/api/bots/{bot_id}/start")
    wait_state(ghc, bot_id, "stopped")
    texts = "\n".join(log_texts(ghc, bot_id))
    assert "ENV " in texts and "good-token" not in texts
    assert "good-token" not in ghc.get(f"/api/bots/{bot_id}/logs/download").text


def test_delete_linked_bot_and_concurrent_ops(ghc, gh, data_dir):
    gh.add_repo("octo/bot", {"main.py": TICKER, "version.txt": "1"})
    connect(ghc)
    bot_id = make_bot_from_repo(ghc)["bot"]["id"]
    ghc.post(f"/api/bots/{bot_id}/start")
    wait_state(ghc, bot_id, "running")
    gh.push("octo/bot", {"version.txt": "2"})
    ghc.post(f"/api/bots/{bot_id}/source/sync")
    ghc.post(f"/api/bots/{bot_id}/stop")  # serialized after the sync
    wait_for(lambda: get_bot(ghc, bot_id)["source"]["syncing"] is False, desc="sync done")
    assert get_bot(ghc, bot_id)["runtime"]["state"] == "stopped"
    assert ghc.delete(f"/api/bots/{bot_id}").json() == {"ok": True}
    assert ctx(ghc).db.get_source(bot_id) is None


# --- migration ----------------------------------------------------------------------------------


V1_SCHEMA = """
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_lower TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '', entrypoint TEXT, args TEXT NOT NULL DEFAULT '',
    extra_packages TEXT NOT NULL DEFAULT '', restart_policy TEXT NOT NULL DEFAULT 'on-failure',
    autostart INTEGER NOT NULL DEFAULT 1, desired_state TEXT NOT NULL DEFAULT 'stopped',
    deps_fingerprint TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE env_vars (bot_id TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE, key TEXT NOT NULL,
    value_enc BLOB NOT NULL, length INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (bot_id, key));
PRAGMA user_version = 1;
"""


def test_v1_database_upgrades(make_client, data_dir, gh):
    data_dir.mkdir(parents=True)
    (data_dir / "bots" / "b_0000abcd" / "src").mkdir(parents=True)
    (data_dir / "bots" / "b_0000abcd" / "src" / "main.py").write_text("print(1)")
    db = sqlite3.connect(str(data_dir / "botdock.db"))
    db.executescript(V1_SCHEMA)
    db.execute("INSERT INTO bots(id, name, name_lower, entrypoint, created_at, updated_at) "
               "VALUES('b_0000abcd', 'Old', 'old', 'main.py', 0, 0)")
    db.commit()
    db.close()
    c = make_client(github_transport=httpx.ASGITransport(app=gh.app), password="seeded-upgrade-pw")
    c.post("/api/auth/login", json={"password": "seeded-upgrade-pw"})
    bots = c.get("/api/bots").json()["bots"]
    assert [b["source"] for b in bots] == [{"type": "upload"}]
    gh.add_repo("octo/bot", {"main.py": "new"})
    connect(c)
    r = c.put("/api/bots/b_0000abcd/source", json={"type": "github", "repo": "octo/bot", "branch": "main"})
    assert r.status_code == 200
    db = sqlite3.connect(str(data_dir / "botdock.db"))
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    db.close()


# --- small items from the frontend report ---------------------------------------------------


def test_stop_cancels_pending_backoff_restart(make_client):
    c = make_client(backoff_base=1.0, backoff_max=1.0)
    c.post("/api/auth/setup", json=setup_body(c, PASSWORD))
    bot_id = create_bot(c, "Crashy", [("crash.py", b"print('crashing', flush=True)\nraise SystemExit(3)\n")])["bot"]["id"]
    c.post(f"/api/bots/{bot_id}/start")
    bot = wait_for(lambda: (b := get_bot(c, bot_id))["runtime"]["next_restart_at"] and b, desc="backoff pending")
    assert bot["runtime"]["state"] == "crashed"
    rt = c.post(f"/api/bots/{bot_id}/stop").json()["bot"]["runtime"]
    assert rt["state"] == "stopped" and rt["next_restart_at"] is None
    n = log_texts(c, bot_id).count("crashing")
    time.sleep(2.0)  # longer than the backoff: no restart may happen
    assert log_texts(c, bot_id).count("crashing") == n
    assert get_bot(c, bot_id)["runtime"]["state"] == "stopped"


def test_downloads_work_as_plain_cookie_links(authed):
    bot_id = create_bot(authed, "Dl", [("p/main.py", b"print(1)"), ("p/a.txt", b"A")])["bot"]["id"]
    del authed.headers["X-BotDock"]  # a plain <a href> sends only the cookie
    assert authed.get(f"/api/bots/{bot_id}/export").status_code == 200
    assert authed.get(f"/api/bots/{bot_id}/logs/download").status_code == 200
    r = authed.get(f"/api/bots/{bot_id}/files/download", params={"path": "a.txt"})
    assert r.status_code == 200 and r.content == b"A"
    authed.cookies.clear()
    assert authed.get(f"/api/bots/{bot_id}/export").status_code == 401


def test_no_credentials_in_logs(ghc, gh, caplog):
    import logging

    from fake_github import CODELOAD_TOKEN

    caplog.set_level(logging.DEBUG)
    gh.add_repo("octo/bot", {"main.py": "v1"})
    connect(ghc)
    make_bot_from_repo(ghc)
    assert "good-token" not in caplog.text and CODELOAD_TOKEN not in caplog.text
