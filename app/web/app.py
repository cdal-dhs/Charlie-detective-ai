from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.delivery import slack_bot as slack_bot_module
from app.healthcheck import health
from app.web.admin import router as admin_router
from app.web.api import router as api_router
from app.web.app_routes import router as app_router
from app.web.auth import router as auth_router

log = structlog.get_logger()


def make_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web_secret_key or "changeme",
        max_age=settings.web_session_ttl_hours * 3600,
        same_site="lax",
        https_only=False,
    )

    static_dir = Path("app/web/static")
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.get("/health")
    def get_health() -> dict:
        snap = health.snapshot()
        all_connected = all(snap["imap"].values()) if snap["imap"] else False
        all_recent = all(s < 600 for s in snap["last_cycle_seconds_ago"].values()) if snap["last_cycle_seconds_ago"] else False
        ok = all_connected and all_recent
        return {"ok": ok, **snap}

    @app.get("/")
    def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/auth/login", status_code=302)

    app.include_router(auth_router)
    app.include_router(app_router)
    app.include_router(admin_router)
    app.include_router(api_router)

    if slack_bot_module.slack_handler is not None:
        @app.post("/slack/events")
        async def slack_events(request: Request):
            return await slack_bot_module.slack_handler.handle(request)

    return app


async def run_web_server(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    config = uvicorn.Config(
        make_app(),
        host=settings.web_bind_host,
        port=settings.web_bind_port,
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
