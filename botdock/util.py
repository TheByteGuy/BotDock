"""Small shared helpers."""

from __future__ import annotations

import time
from datetime import datetime, timezone


class ApiError(Exception):
    """An error that maps directly onto an HTTP JSON error response."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def iso(ts: float | None, millis: bool = False) -> str | None:
    """Unix timestamp -> UTC ISO-8601 with Z suffix."""
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if millis:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso(millis: bool = False) -> str:
    return iso(time.time(), millis=millis)  # type: ignore[return-value]
