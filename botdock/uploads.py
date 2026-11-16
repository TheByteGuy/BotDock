"""Streaming multipart reception, upload planning (skip/strip/.env/zip) and applying to src/.

The request body is parsed incrementally and every file part is written straight to a staging
directory under ``$DATA/tmp`` so large uploads never sit in memory. Nothing in ``src/`` is touched
until the whole upload has been received and validated.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from .paths import UnsafePath, clean_relpath, resolve_inside
from .util import ApiError

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "node_modules", ".idea", ".vscode", "__MACOSX"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = (".pyc", ".pyo")
MAX_FIELDS = 64
MAX_FIELD_BYTES = 64 * 1024
MAX_ENV_FILE_BYTES = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024 * 1024  # generous allowance for part headers on 20k files


@dataclass
class StagedFile:
    name: str
    path: Path
    size: int


@dataclass
class StagedUpload:
    dir: Path
    fields: dict[str, str] = field(default_factory=dict)
    files: list[StagedFile] = field(default_factory=list)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class _Receiver:
    """Holds parser callback state; the parser runs in a worker thread."""

    def __init__(self, staging: StagedUpload, max_bytes: int, max_files: int):
        self.staging = staging
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.total = 0
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._kind: str | None = None  # "file" | "field" | "ignore"
        self._name = ""
        self._filename = ""
        self._fh = None
        self._fpath: Path | None = None
        self._fsize = 0
        self._fbuf = bytearray()

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self._headers = {}
        self._kind = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._header_field).lower()] = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        _, opts = parse_options_header(self._headers.get(b"content-disposition", b"").decode("latin-1"))
        name = opts.get(b"name", b"").decode("utf-8", "replace")
        filename = opts.get(b"filename")
        self._name = name
        if filename is not None:
            fname = filename.decode("utf-8", "replace")
            if name != "files" or fname == "":
                self._kind = "ignore"
                return
            if len(self.staging.files) >= self.max_files:
                raise ApiError(413, f"Too many files (limit {self.max_files})")
            self._kind = "file"
            self._filename = fname
            self._fpath = self.staging.dir / f"f{len(self.staging.files):06d}"
            self._fh = open(self._fpath, "wb")
            self._fsize = 0
        else:
            if len(self.staging.fields) >= MAX_FIELDS:
                raise ApiError(400, "Too many form fields")
            self._kind = "field"
            self._fbuf = bytearray()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._kind == "file":
            n = end - start
            self.total += n
            if self.total > self.max_bytes:
                raise ApiError(413, f"Upload too large (limit {self.max_bytes // (1024 * 1024)} MB)")
            self._fh.write(data[start:end])
            self._fsize += n
        elif self._kind == "field":
            self._fbuf += data[start:end]
            if len(self._fbuf) > MAX_FIELD_BYTES:
                raise ApiError(413, f"Form field {self._name!r} too large")

    def on_part_end(self) -> None:
        if self._kind == "file":
            self._fh.close()
            self._fh = None
            self.staging.files.append(StagedFile(self._filename, self._fpath, self._fsize))
        elif self._kind == "field":
            self.staging.fields[self._name] = self._fbuf.decode("utf-8", "replace")
        self._kind = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


async def receive_multipart(request: Request, tmp_root: Path, max_bytes: int, max_files: int) -> StagedUpload:
    """Stream a multipart/form-data body to disk. Caller must call ``cleanup()`` on the result."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or not params.get(b"boundary"):
        raise ApiError(400, "Expected multipart/form-data")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + MULTIPART_OVERHEAD:
        raise ApiError(413, f"Upload too large (limit {max_bytes // (1024 * 1024)} MB)")

    tmp_root.mkdir(parents=True, exist_ok=True)
    staging = StagedUpload(dir=tmp_root / f"upload-{secrets.token_hex(8)}")
    staging.dir.mkdir(parents=True)
    receiver = _Receiver(staging, max_bytes, max_files)
    parser = MultipartParser(params[b"boundary"], receiver.callbacks())
    try:
        pending = bytearray()
        received = 0
        async for chunk in request.stream():
            received += len(chunk)  # enforced on bytes actually received, not on Content-Length
            if received > max_bytes + MULTIPART_OVERHEAD:
                raise ApiError(413, f"Upload too large (limit {max_bytes // (1024 * 1024)} MB)")
            pending += chunk
            if len(pending) >= 256 * 1024:
                data = bytes(pending)
                pending.clear()
                await asyncio.to_thread(parser.write, data)
        if pending:
            await asyncio.to_thread(parser.write, bytes(pending))
        parser.finalize()
    except ApiError:
        receiver.close()
        staging.cleanup()
        raise
    except Exception as exc:  # malformed body, client disconnect, disk error
        receiver.close()
        staging.cleanup()
        raise ApiError(400, f"Could not read upload ({exc.__class__.__name__})") from exc
    receiver.close()
    return staging


# Planning ---------------------------------------------------------------------------------------


@dataclass
class PlanEntry:
    rel: str
    size: int
    staged: Path | None = None
    zipinfo: zipfile.ZipInfo | None = None


@dataclass
class Plan:
    entries: list[PlanEntry]
    env_text: str | None = None
    single_py: str | None = None
    zf: zipfile.ZipFile | None = None
    tmp_dir: Path | None = None

    def close(self) -> None:
        if self.zf is not None:
            self.zf.close()
            self.zf = None


def is_skipped(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return name in SKIP_FILES or name.lower().endswith(SKIP_SUFFIXES)


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _check_tree_conflicts(rels: list[str]) -> None:
    files = set(rels)
    for rel in rels:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in files:
                raise ApiError(400, f"Upload contains both a file and a folder named {prefix!r}")


def build_plan(
    upload: StagedUpload,
    *,
    max_bytes: int,
    max_files: int,
    bot_mode: bool = True,
    prefix: str = "",
) -> Plan:
    """bot_mode: single-file/zip detection, skip rules, top-level dir stripping and .env import.
    Otherwise (file manager uploads) every file is stored as-is under ``prefix``."""
    files = upload.files
    if not files:
        raise ApiError(400, "No files uploaded")
    plan = Plan(entries=[], tmp_dir=upload.dir)
    candidates: list[PlanEntry] = []

    if bot_mode and len(files) == 1 and files[0].name.lower().endswith(".py"):
        base = clean_relpath(files[0].name.split("/")[-1])
        plan.single_py = base
        plan.entries = [PlanEntry(base, files[0].size, staged=files[0].path)]
        return plan

    if bot_mode and len(files) == 1 and files[0].name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(files[0].path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ApiError(400, "Not a valid zip file") from exc
        plan.zf = zf
        try:
            infos = zf.infolist()
            if len(infos) > max_files * 2:
                raise ApiError(413, f"Too many files in zip (limit {max_files})")
            total = 0
            for info in infos:
                if info.is_dir():
                    clean_relpath(info.filename)  # still reject hostile directory names
                    continue
                if _zip_is_symlink(info):
                    raise ApiError(400, f"Zip contains a symbolic link: {info.filename!r}")
                if info.flag_bits & 0x1:
                    raise ApiError(400, "Encrypted zip files are not supported")
                rel = clean_relpath(info.filename)
                total += info.file_size
                if total > max_bytes:
                    raise ApiError(413, f"Zip contents too large (limit {max_bytes // (1024 * 1024)} MB)")
                candidates.append(PlanEntry(rel, info.file_size, zipinfo=info))
            if len(candidates) > max_files:
                raise ApiError(413, f"Too many files in zip (limit {max_files})")
        except BaseException:
            plan.close()
            raise
    else:
        for f in files:
            rel = clean_relpath(f.name)
            if prefix:
                rel = f"{prefix}/{rel}"
            candidates.append(PlanEntry(rel, f.size, staged=f.path))

    try:
        if bot_mode:
            candidates = [c for c in candidates if not is_skipped(c.rel)]
            if candidates and all("/" in c.rel for c in candidates):
                tops = {c.rel.split("/", 1)[0] for c in candidates}
                if len(tops) == 1:
                    for c in candidates:
                        c.rel = c.rel.split("/", 1)[1]
            env = [c for c in candidates if c.rel == ".env"]
            if env:
                plan.env_text = _read_entry_text(plan, env[-1])
                candidates = [c for c in candidates if c.rel != ".env"]

        dedup: dict[str, PlanEntry] = {}  # last one wins for duplicate paths
        for c in candidates:
            dedup[c.rel] = c
        plan.entries = list(dedup.values())
        _check_tree_conflicts([e.rel for e in plan.entries])
        if not plan.entries and plan.env_text is None:
            raise ApiError(400, "Nothing to upload (all files were skipped)")
    except BaseException:
        plan.close()
        raise
    return plan


def _read_entry_text(plan: Plan, entry: PlanEntry) -> str:
    if entry.size > MAX_ENV_FILE_BYTES:
        raise ApiError(413, ".env file too large")
    if entry.zipinfo is not None:
        assert plan.zf is not None
        data = plan.zf.read(entry.zipinfo)
    else:
        assert entry.staged is not None
        data = entry.staged.read_bytes()
    return data.decode("utf-8", "replace")


# Applying ---------------------------------------------------------------------------------------


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in list(path.iterdir()):
        remove_path(child)


def apply_plan(src: Path, plan: Plan, mode: str = "merge") -> list[str]:
    """Write plan entries into src. Returns the list of written relative paths.

    merge: overwrite the given paths, keep everything else. replace: empty src first.
    """
    if mode not in ("merge", "replace"):
        raise ApiError(400, "mode must be 'merge' or 'replace'")
    src.mkdir(parents=True, exist_ok=True)
    targets = [(entry, resolve_inside(src, entry.rel)) for entry in plan.entries]
    if mode == "merge":
        for entry, dest in targets:
            parts = entry.rel.split("/")
            for i in range(1, len(parts)):
                p = Path(src, *parts[:i])
                if p.exists() and not p.is_dir():
                    raise ApiError(409, f"Cannot create folder {'/'.join(parts[:i])!r}: a file with that name exists")
            if dest.is_dir() and not dest.is_symlink():
                raise ApiError(409, f"Cannot overwrite folder {entry.rel!r} with a file")
    else:
        clear_dir(src)

    written: list[str] = []
    tmp_dir = plan.tmp_dir or src
    for n, (entry, dest) in enumerate(targets):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            resolve_inside(src, entry.rel)  # re-check after creating parents
            if entry.zipinfo is not None:
                assert plan.zf is not None
                tmp = tmp_dir / f"z{n:06d}"
                with plan.zf.open(entry.zipinfo) as fin, open(tmp, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1024 * 1024)
                os.replace(tmp, dest)
            else:
                assert entry.staged is not None
                try:
                    os.replace(entry.staged, dest)
                except OSError:
                    shutil.copyfile(entry.staged, dest)
        except UnsafePath:
            raise
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
            raise ApiError(400, f"Could not extract {entry.rel!r} from zip: {exc}") from exc
        except OSError as exc:
            raise ApiError(409, f"Could not write {entry.rel!r}: {exc.strerror or exc}") from exc
        written.append(entry.rel)
    return written
