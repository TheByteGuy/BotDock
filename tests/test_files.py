from __future__ import annotations

import os

import pytest

from conftest import create_bot, upload


@pytest.fixture
def bot(authed):
    return create_bot(authed, "Files", [("p/main.py", b"print(1)\n"), ("p/cogs/a.py", b"a"), ("p/data/x.json", b"{}"),
                                        ("p/img.bin", b"\x00\x01\x02binary")])["bot"]


def test_list_and_read(authed, bot):
    r = authed.get(f"/api/bots/{bot['id']}/files")
    body = r.json()
    assert body["truncated"] is False
    paths = [(e["path"], e["type"]) for e in body["entries"]]
    assert paths == [("cogs", "dir"), ("cogs/a.py", "file"), ("data", "dir"), ("data/x.json", "file"),
                     ("img.bin", "file"), ("main.py", "file")]
    e = body["entries"][-1]
    assert e["name"] == "main.py" and e["size"] == 9 and e["modified_at"].endswith("Z")
    r = authed.get(f"/api/bots/{bot['id']}/files/content", params={"path": "main.py"})
    assert r.json() == {"path": "main.py", "content": "print(1)\n", "size": 9, "binary": False}
    r = authed.get(f"/api/bots/{bot['id']}/files/content", params={"path": "img.bin"})
    assert r.json()["binary"] is True and r.json()["content"] is None
    r = authed.get(f"/api/bots/{bot['id']}/files/download", params={"path": "img.bin"})
    assert r.content == b"\x00\x01\x02binary" and "attachment" in r.headers["content-disposition"]
    assert authed.get(f"/api/bots/{bot['id']}/files/content", params={"path": "nope.py"}).status_code == 404


def test_write_mkdir_move_delete(authed, bot):
    base = f"/api/bots/{bot['id']}/files"
    r = authed.put(f"{base}/content", json={"path": "new/deep/file.py", "content": "x = 1\n"})
    assert r.status_code == 200 and r.json()["entry"]["path"] == "new/deep/file.py"
    assert authed.get(f"{base}/content", params={"path": "new/deep/file.py"}).json()["content"] == "x = 1\n"
    assert authed.post(f"{base}/mkdir", json={"path": "empty"}).json()["entry"]["type"] == "dir"
    assert authed.post(f"{base}/mkdir", json={"path": "empty"}).status_code == 409
    assert authed.post(f"{base}/move", json={"from": "main.py", "to": "cogs/a.py"}).status_code == 409
    r = authed.post(f"{base}/move", json={"from": "main.py", "to": "moved/main2.py"})
    assert r.status_code == 200 and r.json()["entry"]["path"] == "moved/main2.py"
    assert authed.post(f"{base}/move", json={"from": "cogs", "to": "cogs/inner"}).status_code == 400
    assert authed.post(f"{base}/delete", json={"path": "cogs"}).json() == {"ok": True}
    assert authed.post(f"{base}/delete", json={"path": "cogs"}).status_code == 404
    paths = {e["path"] for e in authed.get(base).json()["entries"]}
    assert "cogs" not in paths and "moved/main2.py" in paths
    assert authed.post(f"{base}/delete", json={"path": ""}).status_code == 400


def test_file_upload_endpoint(authed, bot):
    base = f"/api/bots/{bot['id']}/files"
    r = upload(authed, f"{base}/upload", [("x/y.txt", b"y"), (".env", b"A=1")], dir="data")
    assert r.status_code == 200, r.text
    assert sorted(e["path"] for e in r.json()["entries"]) == ["data/.env", "data/x/y.txt"]
    r = upload(authed, f"{base}/upload", [("root.txt", b"r")], dir="")
    assert r.json()["entries"][0]["path"] == "root.txt"
    assert upload(authed, f"{base}/upload", [("../x.txt", b"r")], dir="").status_code == 400
    assert upload(authed, f"{base}/upload", [("x.txt", b"r")], dir="../..").status_code == 400


@pytest.mark.parametrize("path", [
    "../secret.key", "../../botdock.db", "a/../../x", "/etc/passwd", "C:/Windows/win.ini", "C:\\Windows\\win.ini",
    "..\\..\\botdock.db", "\\\\server\\share", "NUL", "con.txt", "x\x00y", "a:stream",
])
@pytest.mark.usefixtures("windows_rules")
def test_traversal_rejected(authed, bot, path):
    base = f"/api/bots/{bot['id']}/files"
    assert authed.get(f"{base}/content", params={"path": path}).status_code == 400
    assert authed.get(f"{base}/download", params={"path": path}).status_code == 400
    assert authed.put(f"{base}/content", json={"path": path, "content": "x"}).status_code == 400
    assert authed.post(f"{base}/mkdir", json={"path": path}).status_code == 400
    assert authed.post(f"{base}/delete", json={"path": path}).status_code == 400
    assert authed.post(f"{base}/move", json={"from": "main.py", "to": path}).status_code == 400
    assert authed.post(f"{base}/move", json={"from": path, "to": "zzz"}).status_code == 400


def test_symlink_escape_rejected(authed, bot, data_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    src = data_dir / "bots" / bot["id"] / "src"
    try:
        os.symlink(outside, src / "escape", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    base = f"/api/bots/{bot['id']}/files"
    assert authed.get(f"{base}/content", params={"path": "escape/secret.txt"}).status_code == 400
    assert authed.put(f"{base}/content", json={"path": "escape/new.txt", "content": "x"}).status_code == 400
    assert not (outside / "new.txt").exists()
    r = upload(authed, f"/api/bots/{bot['id']}/code", [("escape/evil.py", b"x"), ("other.py", b"")])
    assert r.status_code == 400
    assert not (outside / "evil.py").exists()
