"""Path validation shared by uploads, zip extraction and the file API.

All user supplied paths are posix relative paths inside a bot's ``src/``. Anything that could
escape the directory or misbehave on Windows is rejected rather than "fixed up".
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .util import ApiError

_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"COM{i}" for i in range(10)} | {
    f"LPT{i}" for i in range(10)
} | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_BAD_CHARS = re.compile(r'[<>:"|?*]')
# Windows-only name rules (reserved device names, ':' and friends, trailing dot/space). Applied only
# when BotDock runs on Windows, so a Linux repo with e.g. aux.py syncs fine on the Linux NAS.
# Traversal, absolute paths, drive letters, backslashes and control characters are rejected everywhere.
WINDOWS_NAME_RULES = os.name == "nt"
MAX_PATH_LEN = 1024
MAX_COMPONENT_LEN = 255


class UnsafePath(ApiError):
    def __init__(self, path: str, reason: str):
        super().__init__(400, f"Unsafe path {path!r}: {reason}")


def clean_relpath(raw: str, *, allow_root: bool = False) -> str:
    """Validate a user supplied relative posix path and return it normalised ("a/b/c").

    Returns "" for the root when ``allow_root`` is set. Raises UnsafePath otherwise.
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise UnsafePath(str(raw), "not a string")
    path = raw.strip()
    if path in ("", ".", "/") and allow_root:
        if path == "/":
            raise UnsafePath(raw, "absolute paths are not allowed")
        return ""
    if path == "":
        raise UnsafePath(raw, "empty path")
    if len(path) > MAX_PATH_LEN:
        raise UnsafePath(raw[:80], "path too long")
    if "\\" in path:
        raise UnsafePath(raw, "backslashes are not allowed")
    if path.startswith("/"):
        raise UnsafePath(raw, "absolute paths are not allowed")
    if re.match(r"^[A-Za-z]:(/|$)", path) or (WINDOWS_NAME_RULES and re.match(r"^[A-Za-z]:", path)):
        raise UnsafePath(raw, "drive letters are not allowed")
    parts = [p for p in path.split("/") if p != ""]
    out = []
    for part in parts:
        if part == ".":
            continue
        if part == "..":
            raise UnsafePath(raw, "'..' is not allowed")
        if len(part) > MAX_COMPONENT_LEN:
            raise UnsafePath(raw, "path component too long")
        if _CONTROL_CHARS.search(part):
            raise UnsafePath(raw, "contains control characters")
        if WINDOWS_NAME_RULES:
            if _WINDOWS_BAD_CHARS.search(part):
                raise UnsafePath(raw, "contains characters that are not allowed on Windows")
            if part.endswith(" ") or part.endswith("."):
                raise UnsafePath(raw, "components may not end with a dot or space on Windows")
            stem = part.split(".", 1)[0].upper().rstrip(" ")
            if stem in _RESERVED:
                raise UnsafePath(raw, f"'{part}' is a reserved name on Windows")
        out.append(part)
    if not out:
        if allow_root:
            return ""
        raise UnsafePath(raw, "empty path")
    return "/".join(out)


def _is_within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def resolve_inside(root: Path, rel: str) -> Path:
    """Join a cleaned relative path onto root and make sure it (after resolving symlinks of every
    existing component) is still inside root. Returns the non-resolved joined path."""
    root_real = os.path.realpath(root)
    target = Path(root, *rel.split("/")) if rel else Path(root)
    real = os.path.realpath(target)
    if not _is_within(os.path.normcase(real), os.path.normcase(root_real)):
        raise UnsafePath(rel, "resolves outside the bot directory")
    return target


def safe_path(root: Path, raw: str, *, allow_root: bool = False) -> tuple[str, Path]:
    rel = clean_relpath(raw, allow_root=allow_root)
    return rel, resolve_inside(root, rel)
