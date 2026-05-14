from __future__ import annotations

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language
from app.web.deps import get_db, require_operator
from app.web.utils import audit_log

log = structlog.get_logger()
router = APIRouter(prefix="/api", tags=["api"])
templates = Jinja2Templates(directory="app/web/templates")


_SORTABLE_COLS = {
    "mailbox": "mailbox_name",
    "subject": "subject",
    "sender": "sender",
    "category": "category",
    "status": "status",
    "priority": "priority",
    "date": "processed_at",
}


async def _fetch_mails_partial(
    db: aiosqlite.Connection,
    box: str | None,
    category: str | None,
    status: str | None,
    priority: str | None,
    sort_col: str = "date",
    sort_order: str = "desc",
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

    col = _SORTABLE_COLS.get(sort_col, "processed_at")
    order = "DESC" if sort_order.lower() == "desc" else "ASC"

    sql = (
        "SELECT id, mailbox_name, subject, sender, received_at, category, "
        "status, priority, processed_at, body_preview "
        "FROM mail_processed WHERE " + " AND ".join(where) + " "
        f"ORDER BY {col} {order} LIMIT ?"
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
    sort_col = request.query_params.get("sort") or "date"
    sort_order = request.query_params.get("order") or "desc"

    mails = await _fetch_mails_partial(db, box, category, status, priority, sort_col, sort_order)
    return templates.TemplateResponse(
        request,
        "app/inbox_rows.html",
        {
            "mails": mails,
            "filters": {
                "box": box, "category": category, "status": status,
                "priority": priority, "sort": sort_col, "order": sort_order,
            },
        },
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


def _ai_draft_html(draft_text: str) -> str:
    safe = (
        draft_text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        f'<div class="bg-gray-900 border border-gray-800 rounded-lg p-4" id="ai-draft-section">'
        f'  <div class="flex items-center justify-between mb-3">'
        f'    <div class="flex items-center gap-2">'
        f'      <span class="w-2 h-2 rounded-full bg-green-400"></span>'
        f'      <h2 class="font-semibold">Réponse proposée par Charlie</h2>'
        f'    </div>'
        f'    <div class="flex gap-2">'
        f'      <button type="button" class="px-3 py-1 bg-gray-800 hover:bg-gray-700 rounded text-xs"'
        f'        onclick="navigator.clipboard.writeText(document.getElementById(\'ai-draft-text\').innerText); this.innerText = \'Copié !\'; setTimeout(() => this.innerText = \'Copier\', 1500)">Copier</button>'
        f'    </div>'
        f'  </div>'
        f'  <div id="ai-draft-text" class="bg-gray-950 border border-gray-800 rounded p-3 text-sm text-gray-300 whitespace-pre-wrap max-h-80 overflow-y-auto">{safe}</div>'
        f'</div>'
    )


@router.post("/drafts/{mail_id}/generate")
async def draft_generate(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    async with db.execute(
        "SELECT id, mailbox_name, subject, sender, category, body_preview, ai_draft "
        "FROM mail_processed WHERE id = ?",
        (mail_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    _, mailbox_name, subject, sender, category, body_preview, existing_draft = row

    if existing_draft:
        return HTMLResponse(_ai_draft_html(existing_draft))

    if not body_preview:
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded '
            'text-red-300 text-sm">Pas de contenu disponible pour générer un brouillon.</div>'
        )

    # Find mailbox config
    settings = get_settings()
    mailbox = None
    for mb in settings.mailboxes():
        if mb.name == mailbox_name:
            mailbox = mb
            break
    if mailbox is None:
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded '
            'text-red-300 text-sm">Configuration boîte mail introuvable.</div>'
        )

    try:
        language = detect_language(body_preview, default=mailbox.default_lang)
        result = await generate_draft(
            incoming_subject=subject or "",
            incoming_body=body_preview,
            sender=sender or "",
            mailbox=mailbox,
            language=language,
            category=category or "",
        )
    except Exception as e:
        log.warning("draft_generate.failed", mail_id=mail_id, error=str(e))
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded '
            'text-red-300 text-sm">Échec de la génération du brouillon.</div>'
        )

    await db.execute(
        "UPDATE mail_processed SET ai_draft = ? WHERE id = ?",
        (result.draft, mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_generate", "mail_processed", str(mail_id),
        f"model={result.model_used} lang={language}", ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(_ai_draft_html(result.draft))


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
    form = await request.form()
    body = str(form.get("body", "")).strip()
    extra_msg = ""

    if body:
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
        extra_msg = f" (v{version} sauvegardée)"

    await db.execute(
        "UPDATE mail_processed SET status = 'approved', reviewed_by = ?, "
        "reviewed_at = datetime('now') WHERE id = ?",
        (user["id"], mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "draft_approve", "mail_processed", str(mail_id),
        extra_msg or None, ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        f'Brouillon approuvé.{extra_msg}'
        '</div>'
    )
