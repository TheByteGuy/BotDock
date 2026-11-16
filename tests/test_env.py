from __future__ import annotations

from conftest import setup_body, create_bot

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GhIjKl.super-secret-discord-token-3FYU"


def test_env_crud_and_masking(authed):
    bot_id = create_bot(authed, "Envy", [("main.py", b"")])["bot"]["id"]
    base = f"/api/bots/{bot_id}/env"
    assert authed.get(base).json() == {"vars": []}
    assert authed.put(f"{base}/DISCORD_TOKEN", json={"value": TOKEN}).json() == {"ok": True}
    assert authed.put(f"{base}/SHORT", json={"value": "abc"}).json() == {"ok": True}
    vars_ = {v["key"]: v for v in authed.get(base).json()["vars"]}
    assert vars_["DISCORD_TOKEN"]["masked"] == "•" * 8 + "3FYU"
    assert vars_["DISCORD_TOKEN"]["length"] == len(TOKEN)
    assert vars_["DISCORD_TOKEN"]["updated_at"].endswith("Z")
    assert vars_["SHORT"]["masked"] == "•" * 8
    # list responses never contain values
    assert TOKEN not in authed.get(base).text
    assert TOKEN not in authed.get("/api/bots").text
    assert authed.get(f"{base}/DISCORD_TOKEN").json() == {"key": "DISCORD_TOKEN", "value": TOKEN}
    assert authed.put(f"{base}/SHORT", json={"value": "updated"}).status_code == 200
    assert authed.get(f"{base}/SHORT").json()["value"] == "updated"
    assert authed.delete(f"{base}/SHORT").json() == {"ok": True}
    assert authed.get(f"{base}/SHORT").status_code == 404
    assert authed.delete(f"{base}/SHORT").status_code == 404
    assert authed.get(f"/api/bots/{bot_id}").json()["bot"]["env_count"] == 1


def test_env_validation(authed):
    bot_id = create_bot(authed, "Val", [("main.py", b"")])["bot"]["id"]
    base = f"/api/bots/{bot_id}/env"
    assert authed.put(f"{base}/1BAD", json={"value": "x"}).status_code == 400
    assert authed.put(f"{base}/BAD-KEY", json={"value": "x"}).status_code == 400
    assert authed.put(f"{base}/{'K' * 129}", json={"value": "x"}).status_code == 400
    assert authed.put(f"{base}/BOTDOCK_PASSWORD", json={"value": "x"}).status_code == 400
    assert authed.put(f"{base}/OK", json={"value": "x" * (32 * 1024 + 1)}).status_code == 400
    assert authed.put(f"{base}/OK", json={"value": 5}).status_code == 400
    assert authed.put(f"{base}/OK", json={}).status_code == 400
    assert authed.put(f"/api/bots/b_00000000/env/OK", json={"value": "x"}).status_code == 404


def test_env_import(authed):
    bot_id = create_bot(authed, "Imp", [("main.py", b"")])["bot"]["id"]
    base = f"/api/bots/{bot_id}/env"
    authed.put(f"{base}/EXISTING", json={"value": "old"})
    text = (
        "# comment\n"
        "A=1\n"
        'B="two words" # trailing comment\n'
        "export C='single $quoted'\n"
        "EXISTING=new\n"
        "D=unquoted value # comment\n"
        'E="line1\\nline2"\n'
        "BOTDOCK_X=nope\n"
        "not a pair\n"
        "\n"
    )
    r = authed.post(f"{base}/import", json={"text": text, "overwrite": False})
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["imported"]) == ["A", "B", "C", "D", "E"]
    assert sorted(body["skipped"]) == ["BOTDOCK_X", "EXISTING"]
    got = lambda k: authed.get(f"{base}/{k}").json()["value"]  # noqa: E731
    assert (got("A"), got("B"), got("C"), got("D"), got("E")) == (
        "1", "two words", "single $quoted", "unquoted value", "line1\nline2")
    assert got("EXISTING") == "old"
    r = authed.post(f"{base}/import", json={"text": "EXISTING=new", "overwrite": True})
    assert r.json() == {"imported": ["EXISTING"], "skipped": []}
    assert got("EXISTING") == "new"


def test_env_encrypted_at_rest(authed, data_dir):
    bot_id = create_bot(authed, "Crypt", [("main.py", b"")])["bot"]["id"]
    authed.put(f"/api/bots/{bot_id}/env/DISCORD_TOKEN", json={"value": TOKEN})
    authed.post(f"/api/bots/{bot_id}/env/import", json={"text": "SECOND=another-plaintext-secret-value"})
    raw = b""
    for f in data_dir.glob("botdock.db*"):
        raw += f.read_bytes()
    assert b"DISCORD_TOKEN" in raw  # keys are stored in clear (sanity check that we read the db)
    assert TOKEN.encode() not in raw
    assert b"another-plaintext-secret-value" not in raw
    key_file = data_dir / "secret.key"
    assert key_file.exists() and len(key_file.read_bytes().strip()) == 44


def test_env_survives_restart_with_same_key(make_client):
    c1 = make_client()
    c1.post("/api/auth/setup", json=setup_body(c1, "password123"))
    bot_id = create_bot(c1, "Persist", [("main.py", b"")])["bot"]["id"]
    c1.put(f"/api/bots/{bot_id}/env/K", json={"value": "v"})
    c1.__exit__(None, None, None)
    c2 = make_client()
    c2.post("/api/auth/login", json={"password": "password123"})
    assert c2.get(f"/api/bots/{bot_id}/env/K").json()["value"] == "v"
