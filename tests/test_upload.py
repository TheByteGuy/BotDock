from __future__ import annotations

import pytest

from conftest import setup_body, create_bot, make_zip, upload


def files_of(client, bot_id):
    return {e["path"] for e in client.get(f"/api/bots/{bot_id}/files").json()["entries"] if e["type"] == "file"}


def test_single_file_bot(authed):
    res = create_bot(authed, "Ticker", [("ticker.py", b"print('hi')\n")])
    bot = res["bot"]
    assert bot["id"].startswith("b_") and len(bot["id"]) == 10
    assert bot["entrypoint"] == "ticker.py"
    assert res["detected_entrypoints"] == ["ticker.py"]
    assert res["imported_env_keys"] == []
    assert bot["file_count"] == 1 and bot["size_bytes"] == len(b"print('hi')\n")
    assert bot["restart_policy"] == "on-failure" and bot["autostart"] is True
    assert bot["runtime"]["state"] == "stopped" and bot["runtime"]["pid"] is None
    assert bot["created_at"].endswith("Z") and bot["has_requirements"] is False
    assert files_of(authed, bot["id"]) == {"ticker.py"}


def test_single_py_inside_folder_path_uses_basename(authed):
    res = create_bot(authed, "Nested", [("MyBot/run_me.py", b"pass\n")])
    assert res["bot"]["entrypoint"] == "run_me.py"
    assert files_of(authed, res["bot"]["id"]) == {"run_me.py"}


def test_folder_upload_strip_skip_env(authed):
    files = [
        ("MyBot/main.py", b"import os\n"),
        ("MyBot/bot.py", b"pass\n"),
        ("MyBot/cogs/tickets.py", b"pass\n"),
        ("MyBot/data/state.json", b"{}"),
        ("MyBot/requirements.txt", b"discord.py\n"),
        ("MyBot/.env", b"DISCORD_TOKEN=abc123\n# comment\nexport OTHER='two words'\n"),
        ("MyBot/__pycache__/main.cpython-312.pyc", b"\0\0"),
        ("MyBot/cogs/__pycache__/x.pyc", b"\0"),
        ("MyBot/.git/HEAD", b"ref"),
        ("MyBot/venv/bin/python", b"x"),
        ("MyBot/node_modules/a/index.js", b"x"),
        ("MyBot/.DS_Store", b"x"),
        ("MyBot/Thumbs.db", b"x"),
        ("MyBot/old.pyo", b"x"),
    ]
    res = create_bot(authed, "Big Bot", files)
    bot = res["bot"]
    assert files_of(authed, bot["id"]) == {"main.py", "bot.py", "cogs/tickets.py", "data/state.json", "requirements.txt"}
    assert bot["entrypoint"] == "main.py"
    assert res["detected_entrypoints"] == ["main.py", "bot.py"]
    assert sorted(res["imported_env_keys"]) == ["DISCORD_TOKEN", "OTHER"]
    assert bot["has_requirements"] is True and bot["env_count"] == 2
    assert authed.get(f"/api/bots/{bot['id']}/env/OTHER").json()["value"] == "two words"


def test_folder_without_common_root_is_not_stripped(authed):
    res = create_bot(authed, "Flat", [("a/x.py", b""), ("b/y.py", b""), ("tool.py", b"")])
    assert files_of(authed, res["bot"]["id"]) == {"a/x.py", "b/y.py", "tool.py"}
    assert res["bot"]["entrypoint"] == "tool.py"  # the only top-level .py


def test_ambiguous_entrypoint_is_null_and_override(authed):
    res = create_bot(authed, "Ambiguous", [("x/one.py", b""), ("x/two.py", b"")])
    assert res["bot"]["entrypoint"] is None
    assert res["detected_entrypoints"] == ["one.py", "two.py"]
    res2 = create_bot(authed, "Override", [("x/one.py", b""), ("x/two.py", b"")], entrypoint="two.py")
    assert res2["bot"]["entrypoint"] == "two.py"
    r = upload(authed, "/api/bots", [("x/one.py", b""), ("x/two.py", b"")], name="Bad", entrypoint="nope.py")
    assert r.status_code == 400
    assert len(authed.get("/api/bots").json()["bots"]) == 2  # failed create left nothing behind


def test_zip_upload_strips_and_skips(authed):
    z = make_zip({
        "proj/main.py": b"print(1)\n",
        "proj/utils/helpers.py": b"",
        "proj/.env": b"A=1\nB=\"multi\\nline\"\n",
        "proj/__pycache__/main.cpython-312.pyc": b"",
        "__MACOSX/proj/._main.py": b"",
    })
    res = create_bot(authed, "Zipped", [("proj.zip", z)])
    assert files_of(authed, res["bot"]["id"]) == {"main.py", "utils/helpers.py"}
    assert sorted(res["imported_env_keys"]) == ["A", "B"]
    assert authed.get(f"/api/bots/{res['bot']['id']}/env/B").json()["value"] == "multi\nline"


@pytest.mark.usefixtures("windows_rules")
def test_zip_slip_and_hostile_names_rejected(authed, data_dir):
    hostile = [
        {"../evil.py": b"x"},
        {"ok/../../evil.py": b"x"},
        {"/abs/evil.py": b"x"},
        {"C:/evil.py": b"x"},
        {"a\\..\\evil.py": b"x"},
        {"CON.py": b"x"},
        {"dir/nul": b"x"},
    ]
    for entries in hostile:
        r = upload(authed, "/api/bots", [("bad.zip", make_zip(entries))], name="Evil")
        assert r.status_code == 400, (entries, r.text)
    r = upload(authed, "/api/bots", [("bad.zip", make_zip({"main.py": b""}, symlinks={"link": "/etc/passwd"}))],
               name="Evil")
    assert r.status_code == 400 and "link" in r.json()["detail"]
    assert authed.get("/api/bots").json()["bots"] == []
    assert not (data_dir.parent / "evil.py").exists() and not (data_dir / "evil.py").exists()


@pytest.mark.usefixtures("windows_rules")
def test_folder_upload_traversal_rejected(authed):
    # (python-multipart itself reduces IE-style "C:\dir\x.py" filenames to "x.py", which is safe)
    for name in ["../x.py", "a/../../x.py", "/etc/x.py", "C:/x.py", "a\\b.py", "a/CON", "a/b:c.py"]:
        r = upload(authed, "/api/bots", [(name, b""), ("other.txt", b"")], name="Trav")
        assert r.status_code == 400, (name, r.status_code, r.text)


def test_invalid_zip_and_empty_upload(authed):
    assert upload(authed, "/api/bots", [("x.zip", b"not a zip")], name="Z").status_code == 400
    assert upload(authed, "/api/bots", [], name="Z").status_code == 400
    r = upload(authed, "/api/bots", [("a/__pycache__/x.pyc", b""), ("a/.DS_Store", b"")], name="Z")
    assert r.status_code == 400


def test_name_validation_and_conflict(authed):
    create_bot(authed, "Unique", [("a.py", b"")])
    r = upload(authed, "/api/bots", [("a.py", b"")], name="uNiQuE")
    assert r.status_code == 409
    assert upload(authed, "/api/bots", [("a.py", b"")], name="").status_code == 400
    assert upload(authed, "/api/bots", [("a.py", b"")], name="x" * 61).status_code == 400


def test_limits_413(make_client):
    c = make_client(max_upload_bytes=1000, max_upload_files=3)
    c.post("/api/auth/setup", json=setup_body(c, "password123"))
    r = upload(c, "/api/bots", [("big.py", b"x" * 2000)], name="Big")
    assert r.status_code == 413
    r = upload(c, "/api/bots", [(f"d/f{i}.py", b"") for i in range(4)], name="Many")
    assert r.status_code == 413
    r = upload(c, "/api/bots", [("z.zip", make_zip({f"f{i}.txt": b"" for i in range(5)}))], name="ManyZ")
    assert r.status_code == 413
    r = upload(c, "/api/bots", [("z.zip", make_zip({"f.txt": b"y" * 5000}))], name="BigZ")
    assert r.status_code == 413


def test_merge_keeps_files_replace_removes(authed):
    res = create_bot(authed, "Merge", [("bot/main.py", b"v1"), ("bot/data/db.json", b"{\"n\":1}"),
                                       ("bot/cogs/a.py", b"a")])
    bot_id = res["bot"]["id"]
    r = upload(authed, f"/api/bots/{bot_id}/code", [("bot/main.py", b"v2"), ("bot/cogs/b.py", b"b"),
                                                    ("bot/.env", b"NEWKEY=1\n")])
    assert r.status_code == 200, r.text
    assert r.json()["imported_env_keys"] == ["NEWKEY"]
    assert files_of(authed, bot_id) == {"main.py", "data/db.json", "cogs/a.py", "cogs/b.py"}
    assert authed.get(f"/api/bots/{bot_id}/files/content?path=main.py").json()["content"] == "v2"
    assert authed.get(f"/api/bots/{bot_id}/files/content?path=data/db.json").json()["content"] == '{"n":1}'

    # .env import never overwrites existing keys
    authed.put(f"/api/bots/{bot_id}/env/NEWKEY", json={"value": "keep"})
    r = upload(authed, f"/api/bots/{bot_id}/code", [(".env", b"NEWKEY=changed\n")])
    assert r.status_code == 200 and r.json()["imported_env_keys"] == []
    assert authed.get(f"/api/bots/{bot_id}/env/NEWKEY").json()["value"] == "keep"

    r = upload(authed, f"/api/bots/{bot_id}/code", [("bot/run.py", b"r"), ("bot/x.txt", b"")], mode="replace")
    assert r.status_code == 200, r.text
    assert files_of(authed, bot_id) == {"run.py", "x.txt"}
    assert r.json()["bot"]["entrypoint"] == "run.py"  # old entrypoint vanished -> re-detected
    assert upload(authed, f"/api/bots/{bot_id}/code", [("a.py", b"")], mode="bogus").status_code == 400


def test_merge_conflicts(authed):
    res = create_bot(authed, "Conf", [("p/main.py", b""), ("p/data/x.json", b"")])
    bot_id = res["bot"]["id"]
    r = upload(authed, f"/api/bots/{bot_id}/code", [("p/data", b"file now"), ("p/other.txt", b"")])
    assert r.status_code == 409
    assert files_of(authed, bot_id) == {"main.py", "data/x.json"}


def test_patch_and_get(authed):
    bot = create_bot(authed, "Patchy", [("p/main.py", b""), ("p/bot.py", b"")])["bot"]
    url = f"/api/bots/{bot['id']}"
    r = authed.patch(url, json={"name": "Patched", "description": "d", "entrypoint": "bot.py", "args": "--x 'a b'",
                                "extra_packages": "requests\nrich", "restart_policy": "always", "autostart": False})
    assert r.status_code == 200, r.text
    b = r.json()["bot"]
    assert (b["name"], b["entrypoint"], b["restart_policy"], b["autostart"]) == ("Patched", "bot.py", "always", False)
    assert authed.patch(url, json={"restart_policy": "sometimes"}).status_code == 400
    assert authed.patch(url, json={"entrypoint": "missing.py"}).status_code == 400
    assert authed.patch(url, json={"entrypoint": "../x.py"}).status_code == 400
    assert authed.patch(url, json={"args": "'unbalanced"}).status_code == 400
    assert authed.patch(url, json={"autostart": "yes"}).status_code == 400
    assert authed.patch(url, json={"entrypoint": None}).json()["bot"]["entrypoint"] is None
    create_bot(authed, "Other", [("a.py", b"")])
    assert authed.patch(url, json={"name": "other"}).status_code == 409
    names = [b["name"] for b in authed.get("/api/bots").json()["bots"]]
    assert names == ["Other", "Patched"]
    assert authed.get("/api/bots/b_ffffffff").status_code == 404
    assert authed.get("/api/bots/nonsense").status_code == 404


def test_export_and_delete(authed, data_dir):
    import io
    import zipfile

    bot = create_bot(authed, "Exp", [("p/main.py", b"print(1)"), ("p/data/a.json", b"{}")])["bot"]
    r = authed.get(f"/api/bots/{bot['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="Exp.zip"' in r.headers["content-disposition"]
    assert set(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == {"main.py", "data/a.json"}
    assert authed.delete(f"/api/bots/{bot['id']}").json() == {"ok": True}
    assert not (data_dir / "bots" / bot["id"]).exists()
    assert authed.get(f"/api/bots/{bot['id']}").status_code == 404
    assert authed.delete(f"/api/bots/{bot['id']}").status_code == 404
