"""Host/Origin validation (DNS rebinding), request body caps, setup code and password seeding."""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from .util import ApiError

log = logging.getLogger("botdock")

LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost")
WEAK_PASSWORDS = {
    "change-me-please", "changeme", "change-me", "changemeplease", "password", "password1", "password123",
    "passw0rd", "12345678", "123456789", "1234567890", "admin", "administrator", "botdock", "letmein",
    "qwerty123", "secret", "default",
}
MIN_PASSWORD_LEN = 4
SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PUBLIC_BODY_CAP = 64 * 1024  # login/setup: unauthenticated, so keep tiny
JSON_BODY_CAP = 16 * 1024 * 1024
PUBLIC_BODY_PATHS = {"/api/auth/login", "/api/auth/setup"}


class BodyTooLarge(ApiError):
    def __init__(self) -> None:
        super().__init__(413, "Request body too large")


# Host / Origin ----------------------------------------------------------------------------------


def split_host(value: str) -> tuple[str, str | None]:
    """'Example.LAN:8080' -> ('example.lan', '8080'); '[::1]:80' -> ('::1', '80')."""
    v = value.strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            return "", None
        host, rest = v[1:end], v[end + 1:]
        return host, rest[1:] if rest.startswith(":") else None
    if v.count(":") == 1:
        host, port = v.split(":")
        return host.rstrip("."), port
    if v.count(":") > 1:  # bare IPv6 without brackets
        return v, None
    return v.rstrip("."), None


def host_allowed(host_header: str | None, extra: tuple[str, ...]) -> bool:
    if "*" in extra:
        return True
    if not host_header:
        return False
    host, _ = split_host(host_header)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host or host.endswith(LAN_SUFFIXES):
        return True
    return host in {split_host(h)[0] for h in extra}


def _norm_netloc(scheme: str, netloc: str) -> str:
    host, port = split_host(netloc)
    if port in (None, "") or (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
        return host
    return f"{host}:{port}"


def origin_matches(origin: str, host_header: str, scheme: str) -> bool:
    if not origin or origin == "null":
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return _norm_netloc(parts.scheme, parts.netloc) == _norm_netloc(parts.scheme, host_header) or \
        _norm_netloc(scheme, parts.netloc) == _norm_netloc(scheme, host_header)


# Passwords / setup code ---------------------------------------------------------------------------


def weak_password_reason(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"shorter than {MIN_PASSWORD_LEN} characters"
    if password.strip().lower() in WEAK_PASSWORDS:
        return "a well-known default password"
    return None


def read_password_file(path: str) -> str | None:
    """Read the seed password and delete the file (so bots can't read it later)."""
    p = Path(path)
    try:
        value = p.read_text(encoding="utf-8").strip("\r\n")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Could not read BOTDOCK_PASSWORD_FILE (%s): %s", path, exc.strerror)
        return None
    try:
        p.unlink()
    except OSError as exc:
        log.warning("Could not delete BOTDOCK_PASSWORD_FILE (%s): %s - delete it yourself", path, exc.strerror)
    return value


def new_setup_code() -> str:
    raw = "".join(secrets.choice(SETUP_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value.upper() if c.isalnum())


def write_secret_file(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
