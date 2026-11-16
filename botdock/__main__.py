"""``python -m botdock`` -> uvicorn on BOTDOCK_HOST:BOTDOCK_PORT."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import Settings


class BotDockServer(uvicorn.Server):
    """On SIGINT/SIGTERM, end the long-lived SSE streams right away so uvicorn can drain
    connections and run the lifespan shutdown (which stops every bot)."""

    def __init__(self, config: uvicorn.Config, ctx):
        super().__init__(config)
        self.ctx = ctx

    def handle_exit(self, sig, frame) -> None:
        super().handle_exit(sig, frame)
        self.ctx.begin_shutdown_threadsafe()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    settings = Settings.from_env()
    app = create_app(settings=settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        # "asyncio" = ProactorEventLoop on Windows, SelectorEventLoop elsewhere. Child processes
        # are watched by threads, so any loop works; this just avoids surprises from uvloop.
        loop="asyncio",
        lifespan="on",
        timeout_graceful_shutdown=2,  # + <=5 s per-bot stop grace: fits Docker's 10 s
        server_header=False,
        log_level="info",
    )
    BotDockServer(config, app.state.ctx).run()


if __name__ == "__main__":
    main()
