from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.delivery.slack_notifier import send_slack_message
from app.healthcheck import health
from app.web.deps import get_db, require_admin
from app.web.utils import FernetManager, audit_log

log = structlog.get_logger()
router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/web/templates")

_SETTINGS_KEYS = [
    "llm_provider",
    "llm_base_url",
    "llm_api_key_encrypted",
    "llm_model_general",
    "llm_model_classifier",
    "llm_model_draft",
    "llm_temperature_analysis",
    "llm_temperature_draft",
    "llm_max_tokens",
]


async def _stats(db: aiosqlite.Connection) -> dict:
    today_sql = "date(processed_at) = date('now')"
    stats = {}
    for key, sql in [
        ("total_today", f"SELECT COUNT(*) FROM mail_processed WHERE {today_sql}"),
        ("pending", "SELECT COUNT(*) FROM mail_processed WHERE status = 'pending'"),
        ("approved", "SELECT COUNT(*) FROM mail_processed WHERE status = 'approved'"),
        ("rejected", "SELECT COUNT(*) FROM mail_processed WHERE status = 'rejected'"),
        ("sent", "SELECT COUNT(*) FROM mail_processed WHERE status = 'sent'"),
    ]:
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
            stats[key] = row[0] if row else 0
    return stats


async def _recent_audit(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    async with db.execute(
        "SELECT id, user_id, action, resource_type, resource_id, details, created_at "
        "FROM audit_logs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    cols = ["id", "user_id", "action", "resource_type", "resource_id", "details", "created_at"]
    return [dict(zip(cols, r, strict=True)) for r in rows]


async def _load_settings(db: aiosqlite.Connection) -> dict:
    settings = {}
    async with db.execute("SELECT key, value, is_encrypted FROM app_settings") as cur:
        async for row in cur:
            key, value, is_enc = row
            settings[key] = {"value": value or "", "masked": bool(is_enc)}
    # Ensure all known keys exist with empty defaults
    for key in _SETTINGS_KEYS:
        if key not in settings:
            settings[key] = {"value": "", "masked": "key" in key.lower()}
    return settings


@router.get("/")
async def admin_redirect():
    return RedirectResponse(url="/admin/dashboard", status_code=302)


@router.get("/dashboard")
async def dashboard(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    stats = await _stats(db)
    audit = await _recent_audit(db, 20)
    snap = health.snapshot()
    db_size = Path(get_settings().db_agent_state).stat().st_size
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "stats": stats,
            "audit": audit,
            "health": snap,
            "db_size": db_size,
            "user": user,
        },
    )


@router.get("/settings")
async def settings_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    settings = await _load_settings(db)
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"settings": settings, "user": user},
    )


@router.post("/api/settings")
async def settings_save(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    form = await request.form()
    fernet = FernetManager()

    for key in _SETTINGS_KEYS:
        raw = str(form.get(key, "")).strip()
        if not raw:
            continue
        is_enc = "key" in key.lower() or "token" in key.lower()
        value = fernet.encrypt(raw) if is_enc else raw
        await db.execute(
            "INSERT INTO app_settings (key, value, is_encrypted, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "is_encrypted=excluded.is_encrypted, updated_at=datetime('now'), "
            "updated_by=excluded.updated_by",
            (key, value, int(is_enc), user["id"]),
        )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "settings_update", "app_settings", None,
        None, ip, request.headers.get("user-agent"),
    )
    return HTMLResponse(
        '<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        'Paramètres sauvegardés.'
        '</div>'
    )


@router.post("/api/test-imap/{box_id}")
async def test_imap(
    request: Request,
    box_id: int,
    user: dict = Depends(require_admin),  # noqa: B008
):
    settings = get_settings()
    mailboxes = settings.mailboxes()
    if box_id < 1 or box_id > len(mailboxes):
        raise HTTPException(status_code=400, detail="Invalid mailbox ID")
    mb = mailboxes[box_id - 1]

    try:
        from aioimaplib import aioimaplib
        client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        await client.wait_hello_from_server()
        resp = await client.login(mb.user, mb.app_password)
        await client.logout()
        ok = resp.result == "OK"
    except Exception as e:
        log.warning("admin.test_imap_failed", error=str(e))
        ok = False

    status = "green" if ok else "red"
    msg = "Connexion IMAP OK" if ok else "Échec connexion IMAP"
    return HTMLResponse(
        f'<div class="p-2 bg-{status}-900/40 border border-{status}-800 rounded text-{status}-300 text-sm">{msg}</div>'
    )


@router.post("/api/test-slack")
async def test_slack(
    request: Request,
    user: dict = Depends(require_admin),  # noqa: B008
):
    try:
        await send_slack_message("Test depuis le panel admin Detective.be")
        ok = True
    except Exception as e:
        log.warning("admin.test_slack_failed", error=str(e))
        ok = False

    status = "green" if ok else "red"
    msg = "Message Slack envoyé" if ok else "Échec envoi Slack"
    return HTMLResponse(
        f'<div class="p-2 bg-{status}-900/40 border border-{status}-800 rounded text-{status}-300 text-sm">{msg}</div>'
    )


@router.get("/users")
async def users_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    async with db.execute(
        "SELECT id, email, role, name, is_active, created_at FROM users ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    cols = ["id", "email", "role", "name", "is_active", "created_at"]
    users = [dict(zip(cols, r, strict=True)) for r in rows]
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"users": users, "user": user},
    )


@router.post("/api/users")
async def add_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    form = await request.form()
    email = str(form.get("email", "")).lower().strip()
    role = str(form.get("role", "")).strip()
    name = str(form.get("name", "")).strip()
    if not email or role not in ("super_admin", "operator"):
        raise HTTPException(status_code=400, detail="Invalid input")

    await db.execute(
        "INSERT OR IGNORE INTO users (email, role, name, is_active) VALUES (?, ?, ?, 1)",
        (email, role, name),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "user_add", "users", email,
        None, ip, request.headers.get("user-agent"),
    )
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/api/users/{user_id}/toggle")
async def toggle_user(
    request: Request,
    user_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    admin_user: dict = Depends(require_admin),  # noqa: B008
):
    await db.execute(
        "UPDATE users SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
        (user_id,),
    )
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=302)


@router.get("/audit")
async def audit_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    page = int(request.query_params.get("page", "1"))
    per_page = 50
    offset = (page - 1) * per_page

    action = request.query_params.get("action") or None
    where = ["1=1"]
    params = []
    if action:
        where.append("action = ?")
        params.append(action)

    count_sql = f"SELECT COUNT(*) FROM audit_logs WHERE {' AND '.join(where)}"
    async with db.execute(count_sql, params) as cur:
        row = await cur.fetchone()
        total = row[0] if row else 0

    sql = (
        f"SELECT id, user_id, action, resource_type, resource_id, details, ip_address, "
        f"created_at FROM audit_logs WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    async with db.execute(sql, (*params, per_page, offset)) as cur:
        rows = await cur.fetchall()
    cols = ["id", "user_id", "action", "resource_type", "resource_id", "details", "ip_address", "created_at"]
    logs = [dict(zip(cols, r, strict=True)) for r in rows]

    total_pages = (total + per_page - 1) // per_page
    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "logs": logs,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "action_filter": action,
            "user": user,
        },
    )
