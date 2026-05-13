import asyncio
from datetime import UTC, datetime

import structlog
import uvicorn
from fastapi import FastAPI

from app.config import get_settings

log = structlog.get_logger()


class HealthState:
    def __init__(self) -> None:
        self.last_cycle: dict[str, datetime] = {}
        self.imap_connected: dict[str, bool] = {}

    def mark_cycle(self, mailbox: str) -> None:
        self.last_cycle[mailbox] = datetime.now(UTC)

    def mark_imap(self, mailbox: str, connected: bool) -> None:
        self.imap_connected[mailbox] = connected

    def snapshot(self) -> dict:
        now = datetime.now(UTC)
        return {
            "imap": self.imap_connected,
            "last_cycle_seconds_ago": {
                m: (now - t).total_seconds() for m, t in self.last_cycle.items()
            },
        }


health = HealthState()


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def get_health():
        snap = health.snapshot()
        all_connected = all(snap["imap"].values()) if snap["imap"] else False
        all_recent = all(s < 600 for s in snap["last_cycle_seconds_ago"].values()) if snap[
            "last_cycle_seconds_ago"
        ] else False
        ok = all_connected and all_recent
        return {"ok": ok, **snap}

    return app


async def run_healthcheck_server(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    config = uvicorn.Config(
        make_app(),
        host=settings.healthcheck_host,
        port=settings.healthcheck_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        [serve_task, stop_task], return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        server.should_exit = True
    for t in pending:
        await t
