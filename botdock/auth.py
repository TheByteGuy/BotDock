"""Admin password (scrypt), sessions, login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from .db import Database

COOKIE_NAME = "botdock_session"
CSRF_HEADER = "x-botdock"
PASSWORD_KEY = "admin_password"
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 1024

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=128 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginRateLimiter:
    def __init__(self, max_failures: int, window: float):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._failures.setdefault(ip, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def is_limited(self, ip: str) -> bool:
        with self._lock:
            return len(self._prune(ip, time.time())) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.time()
            self._prune(ip, now).append(now)
            if len(self._failures) > 10_000:  # bound memory
                for key in [k for k, v in self._failures.items() if not v]:
                    del self._failures[key]

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


class AuthService:
    def __init__(self, db: Database, session_days: int, max_failures: int, window: float):
        self.db = db
        self.session_seconds = session_days * 86400
        self.limiter = LoginRateLimiter(max_failures, window)

    # password -----------------------------------------------------------------------
    def is_setup(self) -> bool:
        return self.db.kv_get(PASSWORD_KEY) is not None

    def set_password(self, password: str) -> None:
        self.db.kv_set(PASSWORD_KEY, hash_password(password))

    def check_password(self, password: str) -> bool:
        stored = self.db.kv_get(PASSWORD_KEY)
        return stored is not None and verify_password(password, stored)

    # sessions -----------------------------------------------------------------------
    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        self.db.execute(
            "INSERT INTO sessions(token_hash, created_at, expires_at) VALUES(?, ?, ?)",
            (_token_hash(token), now, now + self.session_seconds),
        )
        return token

    def validate_session(self, token: str | None) -> bool:
        if not token or len(token) > 256:
            return False
        row = self.db.one("SELECT expires_at FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        return row is not None and row["expires_at"] > time.time()

    def delete_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def delete_other_sessions(self, keep_token: str | None) -> None:
        keep = _token_hash(keep_token) if keep_token else ""
        self.db.execute("DELETE FROM sessions WHERE token_hash != ?", (keep,))
