"""Settings, read from BOTDOCK_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    host: str = "0.0.0.0"
    port: int = 8080
    password: str | None = None
    secret_key: str | None = None
    cookie_secure: bool = False
    password_file: str | None = None
    # Extra Host header values accepted besides IPs, localhost, single-label and LAN-suffix names.
    # ("*",) disables the Host check entirely.
    allowed_hosts: tuple[str, ...] = ()
    # Create a per-bot virtualenv and pip-install requirements. When False, bots run on
    # BotDock's own interpreter and dependency installation is skipped (dev / tests).
    use_venv: bool = True

    # GitHub integration. web_url hosts the device flow (/login/device/code); derived from api_url
    # when not given (api.github.com -> github.com, https://ghe/api/v3 -> https://ghe).
    github_api_url: str = "https://api.github.com"
    github_web_url: str | None = None
    github_client_id: str | None = None
    github_first_poll_delay: float = 10.0
    github_timeout: float = 30.0

    # Supervisor timing (overridable in tests).
    startup_grace: float = 3.0
    stop_timeout: float = 10.0
    shutdown_stop_timeout: float = 5.0  # per-bot SIGTERM grace while BotDock itself shuts down
    backoff_base: float = 2.0
    backoff_max: float = 60.0
    stable_uptime: float = 60.0
    max_rapid_failures: int = 10

    # Limits.
    max_upload_bytes: int = 300 * 1024 * 1024
    max_upload_files: int = 20_000
    log_max_bytes: int = 5 * 1024 * 1024
    log_backups: int = 3
    log_buffer_lines: int = 5000

    # Periodic work.
    stats_interval: float = 5.0
    heartbeat_interval: float = 15.0

    session_days: int = 30
    login_max_failures: int = 5
    login_window_seconds: float = 300.0

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.github_api_url = self.github_api_url.rstrip("/")
        if not self.github_web_url:
            api = self.github_api_url
            if api == "https://api.github.com":
                self.github_web_url = "https://github.com"
            elif api.endswith("/api/v3"):
                self.github_web_url = api[: -len("/api/v3")]
            else:
                self.github_web_url = api
        self.github_web_url = self.github_web_url.rstrip("/")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        kwargs: dict = {
            "data_dir": Path(os.environ.get("BOTDOCK_DATA") or "data"),
            "host": os.environ.get("BOTDOCK_HOST") or "0.0.0.0",
            "port": int(os.environ.get("BOTDOCK_PORT") or 8080),
            "password": os.environ.get("BOTDOCK_PASSWORD") or None,
            "secret_key": os.environ.get("BOTDOCK_SECRET_KEY") or None,
            "cookie_secure": _env_bool("BOTDOCK_COOKIE_SECURE", False),
            "password_file": os.environ.get("BOTDOCK_PASSWORD_FILE") or None,
            "allowed_hosts": tuple(h.strip() for h in (os.environ.get("BOTDOCK_ALLOWED_HOSTS") or "").split(",")
                                   if h.strip()),
            "use_venv": _env_bool("BOTDOCK_USE_VENV", True),
            "github_api_url": os.environ.get("BOTDOCK_GITHUB_API_URL") or "https://api.github.com",
            "github_web_url": os.environ.get("BOTDOCK_GITHUB_URL") or None,
            "github_client_id": os.environ.get("BOTDOCK_GITHUB_CLIENT_ID") or None,
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    # Derived paths -----------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "botdock.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def bots_dir(self) -> Path:
        return self.data_dir / "bots"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def bot_dir(self, bot_id: str) -> Path:
        return self.bots_dir / bot_id

    def src_dir(self, bot_id: str) -> Path:
        return self.bots_dir / bot_id / "src"

    def venv_dir(self, bot_id: str) -> Path:
        return self.bots_dir / bot_id / "venv"

    def logs_dir(self, bot_id: str) -> Path:
        return self.bots_dir / bot_id / "logs"
