"""Safe file browser operations inside a bot's src/ directory."""

from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path

from .paths import clean_relpath, resolve_inside, safe_path
from .uploads import SKIP_DIRS, is_skipped, remove_path
from .util import ApiError, iso

MAX_LIST_ENTRIES = 5000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 10 * 1024 * 1024


def entry_for(rel: str, path: Path) -> dict:
    st = path.lstat()
    is_dir = stat.S_ISDIR(st.st_mode)
    return {
        "path": rel,
        "name": rel.rsplit("/", 1)[-1],
        "type": "dir" if is_dir else "file",
        "size": 0 if is_dir else st.st_size,
        "modified_at": iso(st.st_mtime),
    }


def list_entries(src: Path, limit: int = MAX_LIST_ENTRIES) -> tuple[list[dict], bool]:
    """Depth-first listing: within each folder, dirs first then files, case-insensitive order."""
    entries: list[dict] = []
    if not src.is_dir():
        return entries, False

    def walk(dir_path: Path, prefix: str) -> bool:
        try:
            with os.scandir(dir_path) as it:
                items = list(it)
        except OSError:
            return True
        dirs = sorted((e for e in items if e.is_dir(follow_symlinks=False)), key=lambda e: e.name.lower())
        files = sorted((e for e in items if not e.is_dir(follow_symlinks=False)), key=lambda e: e.name.lower())
        for e in dirs + files:
            if len(entries) >= limit:
                return False
            rel = f"{prefix}{e.name}"
            try:
                entries.append(entry_for(rel, Path(e.path)))
            except OSError:
                continue
            if e in dirs and not walk(Path(e.path), rel + "/"):
                return False
        return True

    complete = walk(src, "")
    return entries, not complete


def count_and_size(src: Path) -> tuple[int, int]:
    count = size = 0
    for root, dirs, files in os.walk(src):
        for name in files:
            try:
                size += os.lstat(os.path.join(root, name)).st_size
                count += 1
            except OSError:
                pass
    return count, size


def _existing(src: Path, raw: str) -> tuple[str, Path]:
    rel, path = safe_path(src, raw)
    if not os.path.lexists(path):
        raise ApiError(404, f"Not found: {rel}")
    return rel, path


def read_content(src: Path, raw: str) -> dict:
    rel, path = _existing(src, raw)
    if path.is_dir():
        raise ApiError(400, f"{rel} is a folder")
    size = path.stat().st_size
    result = {"path": rel, "content": None, "size": size, "binary": True}
    if size > MAX_TEXT_BYTES:
        return result
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return result
    result.update(content=text, binary=False)
    return result


def write_content(src: Path, raw: str, content: str) -> dict:
    rel, path = safe_path(src, raw)
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ApiError(413, "File too large for the editor")
    if path.is_dir():
        raise ApiError(409, f"{rel} is a folder")
    _ensure_parent(src, rel)
    tmp = path.with_name(f".{path.name}.botdock-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ApiError(409, f"Could not write {rel}: {exc.strerror or exc}") from exc
    return entry_for(rel, path)


def _ensure_parent(src: Path, rel: str) -> None:
    parts = rel.split("/")
    for i in range(1, len(parts)):
        p = Path(src, *parts[:i])
        if os.path.lexists(p) and not p.is_dir():
            raise ApiError(409, f"{'/'.join(parts[:i])} is a file")
    Path(src, *parts).parent.mkdir(parents=True, exist_ok=True)
    resolve_inside(src, rel)


def make_dir(src: Path, raw: str) -> dict:
    rel, path = safe_path(src, raw)
    if os.path.lexists(path):
        raise ApiError(409, f"{rel} already exists")
    _ensure_parent(src, rel)
    path.mkdir()
    return entry_for(rel, path)


def move(src: Path, raw_from: str, raw_to: str) -> dict:
    rel_from, p_from = _existing(src, raw_from)
    rel_to, p_to = safe_path(src, raw_to)
    if os.path.lexists(p_to) and os.path.normcase(str(p_to)) != os.path.normcase(str(p_from)):
        raise ApiError(409, f"{rel_to} already exists")
    if rel_to == rel_from:
        return entry_for(rel_to, p_to)
    if rel_to.startswith(rel_from + "/"):
        raise ApiError(400, "Cannot move a folder into itself")
    _ensure_parent(src, rel_to)
    try:
        os.rename(p_from, p_to)
    except OSError as exc:
        raise ApiError(409, f"Could not move: {exc.strerror or exc}") from exc
    return entry_for(rel_to, p_to)


def delete(src: Path, raw: str) -> None:
    rel, path = _existing(src, raw)
    try:
        remove_path(path)
    except OSError as exc:
        raise ApiError(409, f"Could not delete {rel}: {exc.strerror or exc}") from exc


def file_for_download(src: Path, raw: str) -> tuple[str, Path]:
    rel, path = _existing(src, raw)
    if path.is_dir():
        raise ApiError(400, f"{rel} is a folder")
    return rel, path


def clean_dir_param(raw: str) -> str:
    return clean_relpath(raw or "", allow_root=True)


def export_zip(src: Path, dest: Path) -> None:
    src_real = os.path.realpath(src)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)) and d not in SKIP_DIRS)
            rel_root = os.path.relpath(root, src)
            if rel_root != "." and not files and not dirs:
                zf.writestr(rel_root.replace(os.sep, "/") + "/", b"")
            for name in sorted(files):
                full = os.path.join(root, name)
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if os.path.commonpath([os.path.normcase(real), os.path.normcase(src_real)]) != os.path.normcase(src_real):
                        continue
                arc = os.path.relpath(full, src).replace(os.sep, "/")
                if is_skipped(arc):  # same skip list as uploads (__pycache__, *.pyc, .git, venv, ...)
                    continue
                try:
                    zf.write(full, arc)
                except OSError:
                    continue
