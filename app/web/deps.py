from __future__ import annotations

import time

import aiosqlite
import structlog
from fastapi import Depends, HTTPException, Request

from app.config import get_settings

log = structlog.get_logger()

_rate_limit_store: dict[str, list[float]] = {}


async def get_db() -> aiosqlite.Connection:
    settings = get_settings()
    db = await aiosqlite.connect(settings.db_agent_state)
    try:
        yield db
    finally:
        await db.close()


async def get_current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    async with db.execute(
        "SELECT id, email, role, name FROM users WHERE id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "role": row[2],
        "name": row[3],
    }


def require_admin(user: dict | None = Depends(get_current_user),  # noqa: B008
) -> dict:
    if user is None or user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_operator(user: dict | None = Depends(get_current_user),  # noqa: B008
) -> dict:
    if user is None or user.get("role") not in ("operator", "super_admin"):
        raise HTTPException(status_code=403, detail="Operator access required")
    return user


async def rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 15 * 60
    limit = 5

    _rate_limit_store.setdefault(ip, [])
    _rate_limit_store[ip] = [t for t in _rate_limit_store[ip] if now - t < window]
    _rate_limit_store[ip].append(now)

    if len(_rate_limit_store[ip]) > limit:
        log.warning("rate_limit.hit", ip=ip, count=len(_rate_limit_store[ip]))
        raise HTTPException(status_code=429, detail="Too many requests")
