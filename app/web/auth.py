from __future__ import annotations

import hashlib
import secrets

import aiosqlite
import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.web.deps import get_db, rate_limit
from app.web.utils import audit_log

log = structlog.get_logger()
router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _whitelist() -> set[str]:
    settings = get_settings()
    emails = {settings.admin_email.lower().strip(), settings.operator_email.lower().strip()}
    return {e for e in emails if e}


async def get_user_by_email(db: aiosqlite.Connection, email: str) -> dict | None:
    async with db.execute(
        "SELECT id, email, role, name FROM users WHERE email = ?", (email.lower().strip(),)
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


async def create_magic_link(
    db: aiosqlite.Connection, user_id: int, ip_address: str | None
) -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    settings = get_settings()
    ttl = settings.magic_link_ttl_minutes

    await db.execute(
        """
        INSERT INTO magic_tokens (
            user_id, token_hash, created_at, expires_at, used_at, ip_address
        )
        VALUES (?, ?, datetime('now'), datetime('now', ?), NULL, ?)
        """,
        (user_id, token_hash, f"+{ttl} minutes", ip_address),
    )
    await db.commit()
    return raw


async def send_magic_link_email(raw_token: str, user_email: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("auth.magic_link_no_resend_key", email=user_email)
        return

    link = (
        f"http://{settings.web_bind_host}:{settings.web_bind_port}"
        f"/auth/verify?token={raw_token}"
    )

    payload = {
        "from": settings.resend_from,
        "to": [user_email],
        "subject": "Votre lien de connexion Detective.be",
        "html": (
            f"<p>Bonjour,</p>"
            f"<p>Voici votre lien de connexion "
            f"(valable {settings.magic_link_ttl_minutes} minutes) :</p>"
            f'<p><a href="{link}">{link}</a></p>'
        ),
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json=payload,
        )
        r.raise_for_status()
    log.info("auth.magic_link_sent", email=user_email)


async def verify_magic_link(
    db: aiosqlite.Connection, raw_token: str, ip_address: str | None
) -> dict | None:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    async with db.execute(
        """
        SELECT id, user_id, expires_at, used_at
        FROM magic_tokens
        WHERE token_hash = ?
          AND expires_at > datetime('now')
        """,
        (token_hash,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    token_id, user_id, _expires_at, used_at = row
    if used_at is not None:
        return None

    await db.execute(
        """
        UPDATE magic_tokens
        SET used_at = datetime('now'), ip_address = COALESCE(?, ip_address)
        WHERE id = ?
        """,
        (ip_address, token_id),
    )

    await db.execute(
        """
        DELETE FROM magic_tokens
        WHERE used_at IS NOT NULL AND created_at < datetime('now', '-7 days')
        """,
    )
    await db.commit()

    async with db.execute(
        "SELECT id, email, role, name FROM users WHERE id = ?", (user_id,)
    ) as cursor:
        user_row = await cursor.fetchone()

    if user_row is None:
        return None
    return {
        "id": user_row[0],
        "email": user_row[1],
        "role": user_row[2],
        "name": user_row[3],
    }


@router.get("/auth/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/auth/login")
async def login_post(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    _: None = Depends(rate_limit),
):
    form = await request.form()
    email = str(form.get("email", "")).lower().strip()

    if email not in _whitelist():
        msg = "Si cette adresse est autorisée, un lien vous a été envoyé."
        return HTMLResponse(f'<p class="text-green-400">{msg}</p>')

    user = await get_user_by_email(db, email)
    if user is None:
        msg = "Si cette adresse est autorisée, un lien vous a été envoyé."
        return HTMLResponse(f'<p class="text-green-400">{msg}</p>')

    ip = request.client.host if request.client else None
    raw = await create_magic_link(db, user["id"], ip)
    await send_magic_link_email(raw, email)
    await audit_log(
        db,
        user["id"],
        "magic_link_requested",
        "auth",
        None,
        None,
        ip,
        request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<p class="text-green-400">Si cette adresse est autorisée, un lien vous a été envoyé.</p>'
    )


@router.get("/auth/verify")
async def verify(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
):
    raw_token = request.query_params.get("token", "")
    ip = request.client.host if request.client else None
    user = await verify_magic_link(db, raw_token, ip)

    if user is None:
        raise HTTPException(status_code=400, detail="Lien invalide ou expiré")

    request.session["user_id"] = user["id"]
    request.session["role"] = user["role"]

    await audit_log(
        db,
        user["id"],
        "login",
        "auth",
        None,
        None,
        ip,
        request.headers.get("user-agent"),
    )

    redirect_url = "/admin" if user["role"] == "super_admin" else "/app"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)
