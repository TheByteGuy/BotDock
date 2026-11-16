"""Fernet key management and encryption of secrets at rest."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["Crypto", "InvalidToken"]


def _load_or_create_key(key_path: Path) -> bytes:
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(key_path), flags, 0o600)
    except FileExistsError:  # created concurrently
        return key_path.read_bytes().strip()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key + b"\n")
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


class Crypto:
    def __init__(self, key_path: Path, env_key: str | None = None):
        if env_key:
            key = env_key.strip().encode()
        else:
            key = _load_or_create_key(key_path)
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "Invalid Fernet key (BOTDOCK_SECRET_KEY or secret.key): must be 32 url-safe base64-encoded bytes"
            ) from exc

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(bytes(token)).decode("utf-8")
