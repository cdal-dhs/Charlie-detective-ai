from __future__ import annotations

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.web.deps import get_db, require_operator
from app.web.utils import audit_log

log = structlog.get_logger()
router = APIRouter(prefix="/api", tags=["api"])
templates = Jinja2Templates(directory="app/web/templates")


async def _fetch_mails_partial(
    db: aiosqlite.Connection,
    box: str | None,
    category: str | None,
    status: str | None,
    priority: str | None,
    limit: int = 50,
) -> list[dict]:
    where = ["1=1"]
    params = []
    if box:
        where.append("mailbox_name = ?")
        params.append(box)
    if category:
        where.append("category = ?")
        params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if priority:
        where.append("priority = ?")
        params.append(priority)

    sql = (
        "SELECT id, mailbox_name, subject, sender, received_at, category, "
        "status, priority, processed_at, body_preview "
        "FROM mail_processed WHERE " + " AND ".join(where) + " "
        "ORDER BY processed_at DESC LIMIT ?"
    )
    params.append(limit)

    async with db.execute(sql, params) as cursor:
        rows = await cursor.fetchall()

    cols = [
        "id", "mailbox_name", "subject", "sender", "received_at",
        "category", "status", "priority", "processed_at", "body_preview",
    ]
    return [dict(zip(cols, row, strict=True)) for row in rows]


@router.get("/health")
def api_health() -> dict:
    return {"ok": True}


@router.get("/inbox")
async def inbox_partial(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
):
    box = request.query_params.get("box") or None
    category = request.query_params.get("category") or None
    status = request.query_params.get("status") or None
    priority = request.query_params.get("priority") or None

    mails = await _fetch_mails_partial(db, box, category, status, priority)
    return templates.TemplateResponse(
        request,
        "app/inbox_rows.html",
        {"mails": mails},
    )


@router.post("/drafts/{mail_id}/save")
async def draft_save(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    form = await request.form()
    body = str(form.get("body", "")).strip()
    if not body:
        raise HTTPException(status_code=400, detail="Body required")

    await db.execute(
        "UPDATE mail_processed SET human_draft = ? WHERE id = ?",
        (body, mail_id),
    )

    async with db.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM draft_versions WHERE mail_processed_id = ?",
        (mail_id,),
    ) as cursor:
        row = await cursor.fetchone()
        version = row[0] if row else 1

    await db.execute(
        "INSERT INTO draft_versions (mail_processed_id, version, body, editor_id, ai_generated) "
        "VALUES (?, ?, ?, ?, ?)",
        (mail_id, version, body, user["id"], 0),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_save", "mail_processed", str(mail_id),
        f"version {version}", ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        f'<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        f'Brouillon sauvegardé (v{version}).'
        f'</div>'
    )


@router.post("/drafts/{mail_id}/regenerate")
async def draft_regenerate(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_regenerate", "mail_processed", str(mail_id),
        "requested", ip, request.headers.get("user-agent"),
    )
    return HTMLResponse(
        '<div class="p-3 bg-yellow-900/40 border border-yellow-800 rounded '
        'text-yellow-300 text-sm">Regeneration requires full body — feature planned V2.</div>'
    )


@router.post("/drafts/{mail_id}/reject")
async def draft_reject(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    await db.execute(
        "UPDATE mail_processed SET status = 'rejected', reviewed_by = ?, "
        "reviewed_at = datetime('now') WHERE id = ?",
        (user["id"], mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_reject", "mail_processed", str(mail_id),
        None, ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-red-900/40 border border-red-800 rounded text-red-300 text-sm">'
        'Brouillon rejeté.'
        '</div>'
    )


@router.post("/drafts/{mail_id}/approve")
async def draft_approve(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    await db.execute(
        "UPDATE mail_processed SET status = 'approved', reviewed_by = ?, "
        "reviewed_at = datetime('now') WHERE id = ?",
        (user["id"], mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_approve", "mail_processed", str(mail_id),
        None, ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        'Brouillon approuvé.'
        '</div>'
    )
