from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from botdock.app import create_app  # noqa: E402
from botdock.config import Settings  # noqa: E402

PASSWORD = "correct horse battery"


def fast_settings(data_dir: Path, **overrides) -> Settings:
    """Settings for tests: no venv/pip (bots run on this interpreter), short timings."""
    kwargs = dict(
        data_dir=data_dir, use_venv=False, startup_grace=0.5, stop_timeout=5.0, backoff_base=0.2,
        backoff_max=1.0, stable_uptime=30.0, max_rapid_failures=4, stats_interval=0.5, heartbeat_interval=1.0,
        github_api_url="http://api.github.test", github_web_url="http://github.test",
        github_first_poll_delay=3600.0,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
def windows_rules(monkeypatch):
    """Force the Windows-only file name rules on (they are off on Linux)."""
    import botdock.paths

    monkeypatch.setattr(botdock.paths, "WINDOWS_NAME_RULES", True)


@pytest.fixture
def posix_rules(monkeypatch):
    import botdock.paths

    monkeypatch.setattr(botdock.paths, "WINDOWS_NAME_RULES", False)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def make_client(data_dir):
    """Factory: make_client(**settings_overrides) -> started TestClient (closed at teardown)."""
    clients = []

    def factory(github_transport=None, **overrides) -> TestClient:
        app = create_app(settings=fast_settings(data_dir, **overrides), github_transport=github_transport)
        client = TestClient(app)
        client.__enter__()
        client.headers["X-BotDock"] = "1"
        clients.append(client)
        return client

    yield factory
    for c in reversed(clients):
        try:
            c.__exit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def setup_body(client: TestClient, password: str) -> dict:
    """First-run setup body including the one-time setup code BotDock generated."""
    return {"password": password, "setup_code": client.app.state.ctx.setup_code}


@pytest.fixture
def authed(client) -> TestClient:
    r = client.post("/api/auth/setup", json=setup_body(client, PASSWORD))
    assert r.status_code == 200, r.text
    return client


def upload(client: TestClient, url: str, files: list[tuple[str, bytes]], **fields):
    parts = [("files", (name, data, "application/octet-stream")) for name, data in files]
    return client.post(url, files=parts, data={k: str(v) for k, v in fields.items()})


def make_zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, target)
    return buf.getvalue()


def create_bot(client: TestClient, name: str, files: list[tuple[str, bytes]], **fields) -> dict:
    r = upload(client, "/api/bots", files, name=name, **fields)
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(fn, timeout: float = 15.0, interval: float = 0.05, desc: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}; last value: {last!r}")


def get_bot(client: TestClient, bot_id: str) -> dict:
    r = client.get(f"/api/bots/{bot_id}")
    assert r.status_code == 200, r.text
    return r.json()["bot"]


def wait_state(client: TestClient, bot_id: str, *states: str, timeout: float = 15.0) -> dict:
    return wait_for(lambda: (b := get_bot(client, bot_id))["runtime"]["state"] in states and b,
                    timeout=timeout, desc=f"state in {states}")


def log_texts(client: TestClient, bot_id: str) -> list[str]:
    r = client.get(f"/api/bots/{bot_id}/logs?tail=5000")
    assert r.status_code == 200, r.text
    return [line["text"] for line in r.json()["lines"]]
