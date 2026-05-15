from __future__ import annotations

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.charlie import BOX_ABBR, ask_charlie
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
    boxes: list[str] | None,
    category: str | None,
    status: str | None,
    priority: str | None,
    q: str | None,
    sort_col: str = "date",
    sort_order: str = "desc",
    limit: int = 50,
) -> list[dict]:
    where = ["1=1"]
    params = []
    if boxes is not None:
        if boxes:
            placeholders = ",".join("?" for _ in boxes)
            where.append(f"mailbox_name IN ({placeholders})")
            params.extend(boxes)
        else:
            where.append("1=0")
    if category:
        where.append("category = ?")
        params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if priority:
        where.append("priority = ?")
        params.append(priority)
    if q:
        where.append("(LOWER(subject) LIKE ? OR LOWER(sender) LIKE ? OR LOWER(body_preview) LIKE ?)")
        like = f"%{q.lower()}%"
        params.extend([like, like, like])

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
    box_raw = request.query_params.get("box")
    if box_raw is None:
        boxes = None
    else:
        boxes = [b for b in box_raw.split(",") if b]
    category = request.query_params.get("category") or None
    status = request.query_params.get("status") or None
    priority = request.query_params.get("priority") or None
    q = request.query_params.get("q") or None
    sort_col = request.query_params.get("sort") or "date"
    sort_order = request.query_params.get("order") or "desc"

    mails = await _fetch_mails_partial(db, boxes, category, status, priority, q, sort_col, sort_order)
    return templates.TemplateResponse(
        request,
        "app/inbox_rows.html",
        {
            "mails": mails,
            "filters": {
                "box": box_raw, "category": category, "status": status,
                "priority": priority, "q": q, "sort": sort_col, "order": sort_order,
            },
            "version": __version__,
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


_STATUSES = ["pending", "approved", "rejected", "sent", "reviewed"]
_PRIORITIES = ["high", "normal", "low"]
_CATEGORIES = [
    "demande_client", "facture", "newsletter", "spam",
    "urgent", "phishing", "rappel", "autre",
]


@router.post("/mails/{mail_id}/status")
async def mail_update_status(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    form = await request.form()
    new_status = str(form.get("status", "")).strip()
    if new_status not in _STATUSES:
        raise HTTPException(status_code=400, detail="Statut invalide")

    await db.execute(
        "UPDATE mail_processed SET status = ?, reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?",
        (new_status, user["id"], mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "status_update", "mail_processed", str(mail_id),
        new_status, ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        f'<form class="inline" hx-post="/api/mails/{mail_id}/status" '
        f'hx-target="this" hx-swap="outerHTML" hx-trigger="change">'
        f'<select name="status" class="bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200">'
        f'<option value="pending" {"selected" if new_status == "pending" else ""}>pending</option>'
        f'<option value="approved" {"selected" if new_status == "approved" else ""}>approved</option>'
        f'<option value="rejected" {"selected" if new_status == "rejected" else ""}>rejected</option>'
        f'<option value="sent" {"selected" if new_status == "sent" else ""}>sent</option>'
        f'<option value="reviewed" {"selected" if new_status == "reviewed" else ""}>reviewed</option>'
        f'</select></form>'
    )


@router.post("/mails/{mail_id}/priority")
async def mail_update_priority(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    form = await request.form()
    new_priority = str(form.get("priority", "")).strip()
    if new_priority not in _PRIORITIES:
        raise HTTPException(status_code=400, detail="Priorité invalide")

    await db.execute(
        "UPDATE mail_processed SET priority = ? WHERE id = ?",
        (new_priority, mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "priority_update", "mail_processed", str(mail_id),
        new_priority, ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        f'<form class="flex items-center gap-1" hx-post="/api/mails/{mail_id}/priority" '
        f'hx-target="this" hx-swap="outerHTML" hx-trigger="change">'
        f'<span class="'
        f"{'text-red-500' if new_priority == 'high' else 'text-yellow-500' if new_priority == 'normal' else 'text-gray-500'}"
        f'">●</span>'
        f'<select name="priority" class="bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200">'
        f'<option value="high" {"selected" if new_priority == "high" else ""}>high</option>'
        f'<option value="normal" {"selected" if new_priority == "normal" else ""}>normal</option>'
        f'<option value="low" {"selected" if new_priority == "low" else ""}>low</option>'
        f'</select></form>'
    )


@router.post("/mails/{mail_id}/category")
async def mail_update_category(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    form = await request.form()
    new_category = str(form.get("category", "")).strip()
    if new_category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail="Catégorie invalide")

    await db.execute(
        "UPDATE mail_processed SET category = ? WHERE id = ?",
        (new_category, mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "category_update", "mail_processed", str(mail_id),
        new_category, ip, request.headers.get("user-agent"),
    )

    badge_class = (
        "bg-blue-600 text-white" if new_category == "demande_client" else
        "bg-red-600 text-white" if new_category == "urgent" else
        "bg-green-600 text-white" if new_category == "newsletter" else
        "bg-yellow-600 text-white" if new_category == "facture" else
        "bg-gray-600 text-white" if new_category == "spam" else
        "bg-purple-600 text-white" if new_category == "phishing" else
        "bg-orange-600 text-white" if new_category == "rappel" else
        "bg-gray-700 text-gray-300"
    )

    options = ""
    for c in _CATEGORIES:
        sel = "selected" if c == new_category else ""
        options += f'<option value="{c}" {sel}>{c}</option>'

    return HTMLResponse(
        f'<form class="inline" hx-post="/api/mails/{mail_id}/category" '
        f'hx-target="this" hx-swap="outerHTML" hx-trigger="change">'
        f'<select name="category" class="bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200">'
        f'{options}'
        f'</select></form>'
    )


# ── Charlie AI Chat ──────────────────────────────────────────────────────────


def _format_rows_html(rows: list[dict]) -> str:
    """Formate les résultats SQL en tableau HTML avec liens cliquables."""
    if not rows:
        return '<p class="text-xs text-gray-500 mt-1">Aucun résultat.</p>'

    headers = list(rows[0].keys())
    has_id = "id" in headers
    header_html = "".join(
        f'<th class="px-4 py-2 text-left text-sm font-medium text-gray-400 border-b border-gray-600 bg-gray-900/50">{h}</th>'
        for h in headers
    )
    rows_html = ""
    for idx, r in enumerate(rows[:20]):
        bg = "bg-gray-900/30" if idx % 2 == 0 else "bg-transparent"
        cells = ""
        for h in headers:
            v = r.get(h)
            val = str(v)[:80] if v is not None else "-"
            if h == "id" and v is not None:
                val = f'<a href="/app/conversation/{v}" target="_blank" class="text-blue-400 hover:underline font-medium">#{v}</a>'
            elif h == "subject" and has_id and r.get("id") is not None:
                val = f'<a href="/app/conversation/{r["id"]}" target="_blank" class="text-blue-400 hover:underline">{val}</a>'
            elif h == "mailbox_name" and v in BOX_ABBR:
                val = BOX_ABBR[v]
            cells += f'<td class="px-4 py-2 text-sm text-gray-200 border-b border-gray-800 whitespace-nowrap">{val}</td>'
        rows_html += f'<tr class="{bg} hover:bg-gray-700/30 transition-colors">{cells}</tr>'

    html = (
        f'<div class="mt-4 overflow-x-auto border border-gray-700 rounded-lg">'
        f'<table class="w-full text-base"><thead><tr>{header_html}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>'
    )
    if len(rows) > 20:
        html += f'<p class="text-xs text-gray-500 mt-1">({len(rows)} résultats — 20 affichés)</p>'
    return html


@router.post("/charlie/ask")
async def charlie_ask(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    form = await request.form()
    question = str(form.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question vide")

    settings = get_settings()
    result = await ask_charlie(question, db_path=settings.db_agent_state)

    results_html = ""
    if result.sql and not result.sql_safe:
        results_html = '<p class="text-xs text-red-400 mt-1">Requête SQL refusée (sécurité).</p>'
    elif result.sql and result.sql_error:
        results_html = f'<p class="text-xs text-red-400 mt-1">Erreur SQL : {result.sql_error}</p>'
    elif result.rows is not None:
        results_html = _format_rows_html(result.rows)

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "charlie_ask", "mail_processed", "",
        f"q={question[:40]} sql={bool(result.sql)}", ip, request.headers.get("user-agent"),
    )

    safe_question = (
        question
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_response = (
        result.response_text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    user_bubble = (
        f'<div class="flex gap-3 justify-end animate-in fade-in slide-in-from-bottom-2">'
        f'<div class="bg-gray-700 rounded-xl px-5 py-3 max-w-[80%] text-base text-gray-100 leading-relaxed">{safe_question}</div>'
        f'</div>'
    )

    copy_btn = (
        f'<button type="button" class="ml-auto text-gray-500 hover:text-gray-300 text-xs flex items-center gap-1 mt-2 charlie-copy">'
        f'<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg> Copier</button>'
    )

    ai_bubble = (
        f'<div class="flex gap-3 animate-in fade-in slide-in-from-bottom-2 charlie-bubble">'
        f'<div class="w-9 h-9 rounded-full bg-purple-600 flex items-center justify-center text-sm font-bold shrink-0 mt-1">AI</div>'
        f'<div class="flex-1 bg-gray-800 rounded-xl px-5 py-4 text-base text-gray-200 leading-relaxed">'
        f'<div class="charlie-text whitespace-pre-wrap">{safe_response}</div>'
        f'{results_html}'
        f'<div class="flex">{copy_btn}</div>'
        f'</div>'
        f'</div>'
    )

    return HTMLResponse(user_bubble + ai_bubble)
