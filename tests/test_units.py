from __future__ import annotations

import pytest

from botdock.envfile import parse_dotenv
from botdock.logstore import LogWriter
from botdock.paths import UnsafePath, clean_relpath
from conftest import create_bot


@pytest.mark.usefixtures("windows_rules")
def test_clean_relpath():
    assert clean_relpath("a/b/c.py") == "a/b/c.py"
    assert clean_relpath("./a//b/") == "a/b"
    assert clean_relpath("", allow_root=True) == ""
    for bad in ["", "..", "a/../b", "/a", "C:/a", "c:", "a\\b", "aux.txt", "LPT1", "a/b./c", "a /b", "x\x00", "a|b"]:
        with pytest.raises(UnsafePath):
            clean_relpath(bad)


@pytest.mark.usefixtures("posix_rules")
def test_clean_relpath_posix_allows_windows_only_names():
    for ok in ["aux.py", "lib/con.txt", "a:b.py", "x/NUL", "weird name .txt", "trailing."]:
        assert clean_relpath(ok) == ok
    # rules that protect against escaping apply on every platform
    for bad in ["..", "a/../b", "/a", "C:/a", "c:", "a\\b", "x\x00", "tab\there"]:
        with pytest.raises(UnsafePath):
            clean_relpath(bad)


def test_parse_dotenv_edge_cases():
    pairs, bad = parse_dotenv('A = 1\r\nexport B="x\\"y"\nC=\'raw \\n\'\nD="unterminated\nE=e # c\nF=#notcomment\n')
    d = dict(pairs)
    assert d["A"] == "1" and d["B"] == 'x"y' and d["C"] == "raw \\n" and d["E"] == "e" and d["F"] == "#notcomment"
    assert "D" in bad


def test_log_rotation(tmp_path):
    w = LogWriter(tmp_path, max_bytes=1000, backups=3)
    for i in range(200):
        w.write([("2026-01-01T00:00:00.000Z", "stdout", f"line {i:04d} " + "x" * 20)])
    files = [p.name for p in w.files_oldest_first()]
    assert files == ["bot.log.3", "bot.log.2", "bot.log.1", "bot.log"]
    assert all(p.stat().st_size <= 1000 for p in w.files_oldest_first())
    tail = w.read_tail(5)
    assert [t[2][:9] for t in tail] == [f"line {i:04d}" for i in range(195, 200)]
    data = b"".join(w.iter_bytes()).decode()
    assert data.rstrip().endswith("line 0199 " + "x" * 20)
    w.clear()
    assert w.files_oldest_first() == []
    w.close()


def test_folder_upload_beyond_starlette_default_limits(authed):
    files = [(f"Many/pkg/m{i:04d}.py", b"") for i in range(1500)] + [("Many/main.py", b"print(1)")]
    res = create_bot(authed, "ManyFiles", files)
    assert res["bot"]["file_count"] == 1501
    assert res["bot"]["entrypoint"] == "main.py"
