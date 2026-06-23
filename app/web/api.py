# ruff: noqa: E501
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.cerveau_client import push_correction
from app.charlie import BOX_ABBR, ask_charlie
from app.charlie_memory import save_feedback
from app.config import get_settings
from app.pipeline.classifier import _is_reply_to_daniel, classify
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language
from app.pipeline.subject_fixer import fix_subject_llm, tag_no_email
from app.web.deps import get_db, require_operator
from app.web.utils import audit_log

log = structlog.get_logger()
router = APIRouter(prefix="/api", tags=["api"])
templates = Jinja2Templates(directory="app/web/templates")

# Masquer les mails traités avant le 15/05/2026 (pré-prod)
_CUTOFF_DATE = "2026-05-20"

# Marqueurs indiquant qu'un mail est une réponse client
_FOLLOWUP_SUBJECT_RE = re.compile(r"^re\s*:\s*", re.IGNORECASE)
_FOLLOWUP_BODY_MARKERS = (
    "voici",
    "en réponse à",
    "comme demandé",
    "comme convenu",
    "cf. ci-joint",
    "vous trouverez ci-joint",
    "re-bonjour",
    "pour compléter",
    "suite à",
    "ci-joint",
    "merci de bien vouloir",
    "je vous transmets",
    "je vous envoie",
    "je joins",
    "en pièce jointe",
    "pj",
    "piece jointe",
    "ci-dessous",
)


async def _is_web_followup(
    db: aiosqlite.Connection,
    sender: str,
    subject: str,
    body: str,
) -> bool:
    """Détecte si le mail est une réponse d'un client à un échange récent.

    Version cockpit web : pas d'objet Message, on travaille avec les champs bruts.
    """
    sender_norm = sender.lower().strip()
    if not sender_norm:
        return False

    # v1.25.7 — shortcut : Re: + citation d'un mail de Daniel (préfixe > + signature
    # cabinet) = réponse à un échange existant, preuve indépendante de l'historique
    # DB. Cf. #606 (Van Houtte). Permet à la régénération cockpit de produire le
    # brouillon ack même sans mail initial du sender en DB.
    if _is_reply_to_daniel(body, sender):
        return True

    is_reply = bool(_FOLLOWUP_SUBJECT_RE.search(subject))
    body_lower = body.lower()
    if any(marker in body_lower for marker in _FOLLOWUP_BODY_MARKERS):
        is_reply = True

    if not is_reply:
        return False

    cutoff = datetime.now(UTC) - timedelta(days=30)
    rfc_re = re.compile(r"[A-Za-z]{3},\s+(\d+)\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    try:
        async with db.execute(
            """
            SELECT received_at
            FROM mail_processed
            WHERE LOWER(sender) = ? AND category = 'demande_client'
            ORDER BY id DESC
            LIMIT 50
            """,
            (sender_norm,),
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("draft_generate.followup_check_failed", error=str(exc))
        return False

    if not rows:
        return False

    for (received_at,) in rows:
        received_at = received_at or ""
        m = rfc_re.match(str(received_at))
        if not m:
            continue
        day, mon_s, year, hh, mm, ss = m.groups()
        try:
            dt = datetime(
                int(year), months[mon_s], int(day), int(hh), int(mm), int(ss), tzinfo=None
            )
        except (KeyError, ValueError):
            continue
        if dt >= cutoff.replace(tzinfo=None):
            return True

    return False


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
    limit: int = 200,
) -> tuple[list[dict], list[dict]]:
    """Retourne (hot_mails, other_mails)."""
    where = ["processed_at >= ?"]
    params = [_CUTOFF_DATE]
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
        where.append(
            "(LOWER(subject) LIKE ? OR LOWER(sender) LIKE ? OR LOWER(body_preview) LIKE ?)"
        )
        like = f"%{q.lower()}%"
        params.extend([like, like, like])

    col = _SORTABLE_COLS.get(sort_col, "processed_at")
    order = "DESC" if sort_order.lower() == "desc" else "ASC"
    cols = [
        "id",
        "mailbox_name",
        "subject",
        "sender",
        "received_at",
        "category",
        "status",
        "priority",
        "processed_at",
        "body_preview",
        "attachment_count",
    ]

    # ── Requête 1 : HOT (demande_client + high + pending) ──
    hot_where = where + [
        "category = 'demande_client'",
        "priority = 'high'",
        "(status = 'pending' OR status IS NULL)",
    ]
    hot_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count "
        "FROM mail_processed m WHERE " + " AND ".join(hot_where) + " "
        f"ORDER BY {col} {order} LIMIT ?"
    )
    hot_params = params.copy()
    hot_params.append(limit)
    async with db.execute(hot_sql, hot_params) as cursor:
        hot_rows = await cursor.fetchall()
    hot_mails = [dict(zip(cols, row, strict=True)) for row in hot_rows]

    # ── Requête 2 : OTHER (tout sauf hot) ──
    other_where = where + [
        "NOT (category = 'demande_client' AND priority = 'high' AND (status = 'pending' OR status IS NULL))"
    ]
    other_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count "
        "FROM mail_processed m WHERE " + " AND ".join(other_where) + " "
        f"ORDER BY (m.status = 'pending') DESC, (m.priority = 'high') DESC, {col} {order} LIMIT ?"
    )
    other_params = params.copy()
    other_params.append(limit)
    async with db.execute(other_sql, other_params) as cursor:
        other_rows = await cursor.fetchall()
    other_mails = [dict(zip(cols, row, strict=True)) for row in other_rows]

    return hot_mails, other_mails


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
    boxes = None if box_raw is None else [b for b in box_raw.split(",") if b]
    category = request.query_params.get("category") or None
    status = request.query_params.get("status") or None
    priority = request.query_params.get("priority") or None
    q = request.query_params.get("q") or None
    sort_col = request.query_params.get("sort") or "date"
    sort_order = request.query_params.get("order") or "desc"

    hot_mails, other_mails = await _fetch_mails_partial(
        db, boxes, category, status, priority, q, sort_col, sort_order
    )
    return templates.TemplateResponse(
        request,
        "app/inbox_rows.html",
        {
            "hot_mails": hot_mails,
            "other_mails": other_mails,
            "filters": {
                "box": box_raw,
                "category": category,
                "status": status,
                "priority": priority,
                "q": q,
                "sort": sort_col,
                "order": sort_order,
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
        db,
        user["id"],
        "draft_save",
        "mail_processed",
        str(mail_id),
        f"version {version}",
        ip,
        request.headers.get("user-agent"),
    )

    return HTMLResponse(
        f'<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        f"Brouillon sauvegardé (v{version})."
        f"</div>"
    )


def _ai_draft_html(draft_text: str) -> str:
    safe = draft_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<div class="bg-gray-900 border border-gray-800 rounded-lg p-4" id="ai-draft-section">'
        f'  <div class="flex items-center justify-between mb-3">'
        f'    <div class="flex items-center gap-2">'
        f'      <span class="w-2 h-2 rounded-full bg-green-400"></span>'
        f'      <h2 class="font-semibold">Réponse proposée par Charlie</h2>'
        f"    </div>"
        f'    <div class="flex gap-2">'
        f'      <button type="button" class="px-3 py-1 bg-gray-800 hover:bg-gray-700 rounded text-xs"'
        f"        onclick=\"navigator.clipboard.writeText(document.getElementById('ai-draft-text').innerText); this.innerText = 'Copié !'; setTimeout(() => this.innerText = 'Copier', 1500)\">Copier</button>"
        f"    </div>"
        f"  </div>"
        f'  <div id="ai-draft-text" class="bg-gray-950 border border-gray-800 rounded p-3 text-sm text-gray-300 whitespace-pre-wrap max-h-80 overflow-y-auto">{safe}</div>'
        f"</div>"
    )


@router.post("/drafts/{mail_id}/generate")
async def draft_generate(
    request: Request,
    mail_id: int,
    force: bool = False,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    async with db.execute(
        "SELECT id, mailbox_name, subject, sender, category, body_preview, body, ai_draft "
        "FROM mail_processed WHERE id = ?",
        (mail_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    _, mailbox_name, subject, sender, category, body_preview, body, existing_draft = row

    if existing_draft and not force:
        return HTMLResponse(_ai_draft_html(existing_draft))

    # v1.21.0 — utiliser le body complet si dispo (au lieu de body_preview tronqué 2K)
    full_body = body or body_preview or ""
    if not full_body:
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

    # Détection follow-up : si le mail ressemble à une réponse client et que
    # l'expéditeur a un demande_client récent, on génère le brouillon court.
    is_followup = await _is_web_followup(db, sender or "", subject or "", full_body)

    # v1.25.2 — reclassifie AVANT de (re)générer. Sans ça, un mail mal classé
    # (ex: phishing resté phishing alors que le hardening v1.24.0 _has_strong_
    # human_demand devrait le remonter en demande_client) recevrait un brouillon
    # LLM inadapté via la branche `else` de generate_draft. Cf. #614 (Serge M) :
    # le retry cockpit avait régénéré un brouillon LLM hybride incomplet parce
    # que #614 était resté classé phishing en base.
    try:
        new_category = await classify(subject or "", full_body, sender or "")
    except Exception as exc:
        log.warning("draft_generate.reclassify_failed", mail_id=mail_id, error=str(exc))
        new_category = category or ""
    if new_category != (category or ""):
        await db.execute(
            "UPDATE mail_processed SET category = ?, status = 'pending', "
            "priority = 'high' WHERE id = ?",
            (new_category, mail_id),
        )
        await db.commit()
        log.info(
            "draft_generate.reclassified",
            mail_id=mail_id,
            old=category,
            new=new_category,
        )
        category = new_category

    # v1.25.2 — garde anti-brouillon-LLM-inadapté : on ne génère un brouillon
    # QUE pour les catégories qui appellent le builder déterministe (draft_
    # categories). Pour les autres (phishing, spam, facture…), aucune réponse
    # n'est attendue — retourner un message clair au lieu d'un brouillon LLM.
    draft_cats = {c.strip().lower() for c in settings.draft_categories.split(",") if c.strip()}
    if (category or "").lower() not in draft_cats:
        ip = request.client.host if request.client else None
        await audit_log(
            db,
            user["id"],
            "draft_generate_skip",
            "mail_processed",
            str(mail_id),
            f"category={category}",
            ip,
            request.headers.get("user-agent"),
        )
        return HTMLResponse(
            '<div class="p-3 bg-yellow-900/40 border border-yellow-800 rounded '
            f'text-yellow-300 text-sm">Mail classé « {category} » — aucune '
            "génération de brouillon pour cette catégorie (le classifier a été "
            "réappliqué).</div>"
        )

    try:
        language = detect_language(full_body, default=mailbox.default_lang)
        result = await generate_draft(
            incoming_subject=subject or "",
            incoming_body=full_body,
            sender=sender or "",
            mailbox=mailbox,
            language=language,
            category=category or "",
            is_followup_response=is_followup,
        )
    except Exception as e:
        log.warning("draft_generate.failed", mail_id=mail_id, error=str(e))
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded '
            'text-red-300 text-sm">Échec de la génération du brouillon.</div>'
        )

    await db.execute(
        "UPDATE mail_processed SET ai_draft = ?, draft_generated = 1 WHERE id = ?",
        (result.draft, mail_id),
    )
    await db.commit()

    ip = request.client.host if request.client else None
    await audit_log(
        db,
        user["id"],
        "draft_generate",
        "mail_processed",
        str(mail_id),
        f"model={result.model_used} lang={language} force={force}",
        ip,
        request.headers.get("user-agent"),
    )

    return HTMLResponse(_ai_draft_html(result.draft))


@router.post("/drafts/{mail_id}/retry")
async def draft_retry(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    """Force la régénération d'un brouillon, même si un brouillon existe déjà.

    Cas d'usage : un mail a été classifié `demande_client` mais le brouillon
    n'a pas été généré (ex: cycle interrompu, exception silencieuse).
    """
    return await draft_generate(request=request, mail_id=mail_id, force=True, db=db, user=user)


@router.post("/mails/{mail_id}/fix-subject")
async def mail_fix_subject(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    """Corrige un sujet incohérent ou illisible (homoglyphes, forwarder WP) via LLM.

    v1.25.4 — rétrocorrection des anciens mails : (1) reformulation LLM du sujet
    pour refléter la demande réelle (#614 homoglyphes, #515 forwarder WP
    « Réinitialisation du mot de passe »), (2) tag [NO_EMAIL_IN_THE_FORM] si le
    sender est un forwarder WP (pas d'email client, vrai contact = téléphone).
    Le sujet original est conservé dans l'audit log (forensic/debug). Dégradation
    silencieuse si le LLM échoue ET le sender n'est pas un forwarder WP.
    """
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    async with db.execute(
        "SELECT subject, body, sender FROM mail_processed WHERE id = ?", (mail_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Mail not found")
    original_subject, body, sender = row
    original_subject = original_subject or ""
    sender = sender or ""
    body_preview = (body or "")[:600]

    if not original_subject:
        await audit_log(
            db,
            user["id"],
            "subject_fix_empty",
            "mail_processed",
            str(mail_id),
            "no subject",
            ip,
            ua,
        )
        return HTMLResponse('<span class="text-gray-400 italic">(aucun sujet)</span>')

    # 1) Reformulation LLM (homoglyphes OU sujet non-représentatif).
    fixed = await fix_subject_llm(original_subject, body_preview)
    base = fixed if fixed else original_subject
    # 2) Tag [NO_EMAIL_IN_THE_FORM] si forwarder WP (déterministe, sans LLM).
    tagged = tag_no_email(base, sender)

    if tagged == original_subject:
        # Ni LLM ni tag n'ont rien changé → noop.
        await audit_log(
            db,
            user["id"],
            "subject_fix_noop",
            "mail_processed",
            str(mail_id),
            f"no improvement; original={original_subject[:120]!r}",
            ip,
            ua,
        )
        return HTMLResponse(
            f'<span class="text-yellow-300">{original_subject}</span>'
            f'<div class="text-xs text-yellow-500/70 mt-1">'
            "Aucune correction proposée (LLM a échoué ou sujet déjà représentatif).</div>"
        )

    await db.execute("UPDATE mail_processed SET subject = ? WHERE id = ?", (tagged, mail_id))
    await db.commit()
    await audit_log(
        db,
        user["id"],
        "subject_fixed",
        "mail_processed",
        str(mail_id),
        f"original={original_subject[:120]!r} -> fixed={tagged[:120]!r}",
        ip,
        ua,
    )
    log.info(
        "subject_fixed_cockpit",
        mail_id=mail_id,
        subject_original=original_subject[:120],
        subject_fixed=tagged[:120],
        llm_rephrased=bool(fixed),
        wp_tagged=(tagged != base),
    )
    return HTMLResponse(
        f'<span class="text-green-300">{tagged}</span>'
        f'<div class="text-xs text-gray-500/70 mt-1">Sujet corrigé</div>'
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
        db,
        user["id"],
        "draft_regenerate",
        "mail_processed",
        str(mail_id),
        "requested",
        ip,
        request.headers.get("user-agent"),
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
        db,
        user["id"],
        "draft_reject",
        "mail_processed",
        str(mail_id),
        None,
        ip,
        request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-red-900/40 border border-red-800 rounded text-red-300 text-sm">'
        "Brouillon rejeté."
        "</div>"
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
        db,
        user["id"],
        "draft_approve",
        "mail_processed",
        str(mail_id),
        extra_msg or None,
        ip,
        request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        f"Brouillon approuvé.{extra_msg}"
        "</div>"
    )


_STATUSES = ["pending", "approved", "rejected", "sent", "reviewed"]
_PRIORITIES = ["high", "normal", "low"]
_CATEGORIES = [
    "demande_client",
    "facture",
    "newsletter",
    "spam",
    "urgent",
    "phishing",
    "rappel",
    "autre",
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
        db,
        user["id"],
        "status_update",
        "mail_processed",
        str(mail_id),
        new_status,
        ip,
        request.headers.get("user-agent"),
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
        f"</select></form>"
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
        db,
        user["id"],
        "priority_update",
        "mail_processed",
        str(mail_id),
        new_priority,
        ip,
        request.headers.get("user-agent"),
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
        f"</select></form>"
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
        db,
        user["id"],
        "category_update",
        "mail_processed",
        str(mail_id),
        new_category,
        ip,
        request.headers.get("user-agent"),
    )

    options = ""
    for c in _CATEGORIES:
        sel = "selected" if c == new_category else ""
        options += f'<option value="{c}" {sel}>{c}</option>'

    return HTMLResponse(
        f'<form class="inline" hx-post="/api/mails/{mail_id}/category" '
        f'hx-target="this" hx-swap="outerHTML" hx-trigger="change">'
        f'<select name="category" class="bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200">'
        f"{options}"
        f"</select></form>"
    )


# ── Charlie AI Chat ──────────────────────────────────────────────────────────


def _format_rows_html(rows: list[dict]) -> str:
    """Formate les résultats SQL en tableau HTML avec liens cliquables et date."""
    if not rows:
        return ""  # pas de tableau quand 0 résultat — la réponse textuelle suffit

    headers = list(rows[0].keys())
    has_id = "id" in headers
    # Les résultats historiques (archives boite1/2/3) ont une colonne source_db —
    # leur id n'est PAS un mail_id de mail_processed, on ne crée pas de liens.
    is_historical = "source_db" in headers
    # Reorder: date first, then id, subject, sender, others
    priority = ["received_at", "id", "subject", "sender"]
    ordered = [h for h in priority if h in headers] + [h for h in headers if h not in priority]

    header_html = "".join(
        f'<th class="px-4 py-2 text-left text-sm font-medium text-gray-400 border-b border-gray-600 bg-gray-900/50">{h}</th>'
        for h in ordered
    )
    rows_html = ""
    for idx, r in enumerate(rows[:20]):
        bg = "bg-gray-900/30" if idx % 2 == 0 else "bg-transparent"
        cells = ""
        for h in ordered:
            v = r.get(h)
            val = str(v)[:80] if v is not None else "-"
            if h == "received_at" and v:
                val = str(v)[:16].replace("T", " ")  # 2026-05-15T10:30 → 2026-05-15 10:30
            if h == "id" and v is not None:
                if is_historical:
                    val = f'<span class="text-gray-500 font-medium">#{v}</span>'
                else:
                    val = f'<a href="/app/conversation/{v}" target="_blank" class="text-blue-400 hover:underline font-medium">#{v}</a>'
            elif h == "subject" and has_id and r.get("id") is not None and not is_historical:
                val = f'<a href="/app/conversation/{r["id"]}" target="_blank" class="text-blue-400 hover:underline">{val}</a>'
            elif h == "mailbox_name" and v in BOX_ABBR:
                val = BOX_ABBR[v]
            cells += f'<td class="px-4 py-2 text-sm text-gray-200 border-b border-gray-800 whitespace-nowrap">{val}</td>'
        rows_html += f'<tr class="{bg} hover:bg-gray-700/30 transition-colors">{cells}</tr>'

    html = (
        f'<div class="mt-4 overflow-x-auto border border-gray-700 rounded-lg">'
        f'<table class="w-full text-base"><thead><tr>{header_html}</tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )
    if len(rows) > 20:
        html += f'<p class="text-xs text-gray-500 mt-1">({len(rows)} résultats — 20 affichés)</p>'
    return html


@router.post("/charlie/ask")
async def charlie_ask(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> JSONResponse:
    form = await request.form()
    question = str(form.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question vide")

    history_raw = str(form.get("history", "")).strip()
    history = None
    if history_raw:
        try:
            history = json.loads(history_raw)
            if len(history) > 20:
                history = history[-20:]
        except Exception:
            history = None

    settings = get_settings()
    result = await ask_charlie(question, db_path=settings.db_agent_state, history=history)

    results_html = ""
    if result.sql and not result.sql_safe:
        results_html = '<p class="text-xs text-red-400 mt-1">Requête SQL refusée (sécurité).</p>'
    elif result.sql and result.sql_error:
        results_html = f'<p class="text-xs text-red-400 mt-1">Erreur SQL : {result.sql_error}</p>'
    elif result.rows is not None and not result.hide_rows:
        # Ignorer les résultats COUNT(*) mono-cellule : déjà inclus dans response_text
        if not (len(result.rows) == 1 and len(result.rows[0]) == 1):
            results_html = _format_rows_html(result.rows)
    # Si pas de rows SQL mais des archives historiques → message textuel de sources
    # (les archives sont anonymisées — on n'affiche pas le tableau brut)
    if not results_html and result.archive_rows and not result.hide_rows:
        arc_cnt = len(result.archive_rows)
        results_html = (
            f'<p class="text-xs text-gray-500 mt-2">'
            f"📎 {arc_cnt} email(s) source(s) trouvé(s) dans les archives historiques"
            f"</p>"
        )

    vault_html = ""
    if result.vault_notes:
        items_html = ""
        for note in result.vault_notes:
            # --- Parse frontmatter YAML pour un affichage propre ---
            fm: dict = {}
            body_text = note.content
            if body_text.startswith("---"):
                parts = body_text.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            fm[k.strip()] = v.strip().strip('"').strip("'")
                    body_text = parts[2].strip()

            date_str = fm.get("date", "")
            direction = fm.get("direction", "")
            msg_type = fm.get("type", "")

            # --- Extraction sujet depuis le body (première ligne significative) ---
            subject = ""
            for line in body_text.splitlines()[:5]:
                line_clean = line.strip()
                if line_clean and not line_clean.startswith("---") and len(line_clean) > 3:
                    subject = line_clean[:80]
                    break
            if not subject:
                subject = note.path.split("/")[-1].replace(".md", "")[:60]

            # --- Indicateur direction ---
            arrow = "📥" if direction == "in" else "📤" if direction == "out" else "📝"

            # --- Preview body (après le frontmatter) ---
            preview = (
                body_text[:250].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

            msg_type_tag = f'<span class="text-gray-600">[{msg_type}]</span>' if msg_type else ""
            items_html += (
                f'<div class="mt-2 text-xs bg-gray-900 rounded px-3 py-2 border-l-2 border-purple-600">'
                f'<div class="flex items-center gap-2 mb-1">'
                f'<span class="text-purple-400 font-mono">{arrow}</span>'
                f'<span class="text-purple-300 font-medium">{subject}</span>'
                f"</div>"
                f'<div class="flex items-center gap-2 text-gray-500 mb-1">'
                f"<span>{date_str}</span>"
                f"{msg_type_tag}"
                f"</div>"
                f'<div class="text-gray-400 whitespace-pre-wrap">{preview}…</div>'
                f"</div>"
            )
        vault_html = (
            f'<div class="mt-4 border-t border-gray-700 pt-3">'
            f'<div class="text-xs text-purple-400 font-semibold mb-1">📚 Sources Cerveau2 '
            f"({len(result.vault_notes)} note(s))</div>"
            f"{items_html}"
            f"</div>"
        )

    ip = request.client.host if request.client else None
    await audit_log(
        db,
        user["id"],
        "charlie_ask",
        "mail_processed",
        "",
        f"q={question[:40]} sql={bool(result.sql)}",
        ip,
        request.headers.get("user-agent"),
    )

    safe_question = question.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_response = (
        result.response_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    # Échapper pour les attributs HTML (value="...")
    html_q = safe_question.replace('"', "&quot;").replace("'", "&#39;")
    html_r = safe_response.replace('"', "&quot;").replace("'", "&#39;")

    user_bubble = (
        f'<div class="flex gap-3 justify-end animate-in fade-in slide-in-from-bottom-2">'
        f'<div class="bg-gray-700 rounded-xl px-5 py-3 max-w-[80%] text-base text-gray-100 leading-relaxed">{safe_question}</div>'
        f"</div>"
    )

    copy_btn = (
        '<button type="button" class="ml-auto text-gray-500 hover:text-gray-300 text-xs flex items-center gap-1 mt-2 charlie-copy">'
        '<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg> Copier</button>'
    )

    feedback_id = f"fb-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    feedback_html = (
        f'<div class="mt-3 pt-2 border-t border-gray-700/50">'
        f'<div class="flex items-center gap-2 text-xs">'
        f'<span class="text-gray-500">Cette réponse vous a-t-elle aidé ?</span>'
        # « Bonne réponse » : remplace le formulaire entier par le message de succès + désactive le bouton pendant l'envoi
        f'<form hx-post="/api/charlie/feedback" hx-target="this" hx-swap="outerHTML" hx-disabled-elt="find button[type=submit]" class="inline">'
        f'<input type="hidden" name="question" value="{html_q}">'
        f'<input type="hidden" name="response" value="{html_r}">'
        f'<input type="hidden" name="feedback" value="good">'
        f'<button type="submit" class="text-green-400 hover:text-green-300 flex items-center gap-1 transition-colors">'
        f'<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>'
        f"Bonne réponse"
        f"</button></form>"
        f'<button type="button" id="{feedback_id}-toggle" class="text-red-400 hover:text-red-300 flex items-center gap-1 transition-colors"'
        f" onclick=\"document.getElementById('{feedback_id}-form').classList.remove('hidden'); "
        f"document.getElementById('{feedback_id}-toggle').classList.add('hidden');\">"
        f'<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>'
        f"À corriger"
        f"</button>"
        f"</div>"
        f'<div id="{feedback_id}-form" class="hidden mt-2">'
        # Correction : remplace le bloc entier par le message de succès + désactive le bouton pendant l'envoi
        f'<form hx-post="/api/charlie/feedback" hx-target="#{feedback_id}-form" hx-swap="outerHTML" hx-disabled-elt="find button[type=submit]">'
        f'<input type="hidden" name="question" value="{html_q}">'
        f'<input type="hidden" name="response" value="{html_r}">'
        f'<input type="hidden" name="feedback" value="bad">'
        f'<textarea name="corrected_response" rows="2" required class="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-xs text-gray-200" '
        f'placeholder="Votre correction..."></textarea>'
        f'<div class="flex justify-end mt-1">'
        f'<button type="submit" class="px-3 py-1 bg-purple-700 hover:bg-purple-600 rounded text-xs text-white">Envoyer correction</button>'
        f"</div>"
        f"</form>"
        f"</div>"
        f"</div>"
    )

    ai_bubble = (
        f'<div class="flex gap-3 animate-in fade-in slide-in-from-bottom-2 charlie-bubble">'
        f'<div class="w-9 h-9 rounded-full bg-purple-600 flex items-center justify-center text-sm font-bold shrink-0 mt-1">AI</div>'
        f'<div class="flex-1 bg-gray-800 rounded-xl px-5 py-4 text-base text-gray-200 leading-relaxed">'
        f'<div class="charlie-text whitespace-pre-wrap">{safe_response}</div>'
        f"{results_html}"
        f"{vault_html}"
        f'<div class="flex">{copy_btn}</div>'
        f"{feedback_html}"
        f"</div>"
        f"</div>"
    )

    return JSONResponse({"html": user_bubble + ai_bubble, "response_text": result.response_text})


@router.post("/charlie/feedback")
async def charlie_feedback(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
) -> HTMLResponse:
    """Enregistre un feedback (good/bad) sur une réponse de Charlie."""
    form = await request.form()
    question = str(form.get("question", "")).strip()
    response = str(form.get("response", "")).strip()
    feedback = str(form.get("feedback", "")).strip()
    corrected_response = str(form.get("corrected_response", "")).strip() or None

    if not question or not response or feedback not in ("good", "bad"):
        return HTMLResponse('<span class="text-xs text-red-400">Données manquantes.</span>')

    # Extraire le dossier_id de la question pour lier la correction
    dossier_id = None
    m = re.search(
        r"(?i:dossier|affaire|projet|enquete|investigation)[\s:]+([A-Z][a-zA-Z0-9]{2,})", question
    )
    if m:
        dossier_id = m.group(1)
    else:
        m = re.search(r"#([A-Z][A-Z0-9]{2,})", question)
        if m:
            dossier_id = m.group(1)

    settings = get_settings()
    try:
        await save_feedback(
            db_path=settings.db_agent_state,
            question=question,
            response=response,
            feedback=feedback,
            corrected_response=corrected_response,
            dossier_id=dossier_id,
        )
    except Exception as e:
        log.warning("charlie.feedback_failed", error=str(e))
        return HTMLResponse(
            '<span class="text-xs text-red-400">Erreur lors de l\'enregistrement.</span>'
        )

    # Pousse la correction vers Cerveau2 (fire-and-forget, dégradation silencieuse)
    corrected = corrected_response if corrected_response else response
    if corrected:
        try:
            await push_correction(
                question=question,
                corrected_response=corrected,
                original_response=response,
                dossier_id=dossier_id,
                tags=["feedback", feedback],
                base_url=settings.cerveau2_base_url,
                api_secret=settings.cerveau2_api_secret,
            )
        except Exception as e:
            log.warning("charlie.cerveau2_push_failed", error=str(e), dossier_id=dossier_id)

    ip = request.client.host if request.client else None
    await audit_log(
        db,
        user["id"],
        "charlie_feedback",
        "charlie_memory",
        "",
        f"feedback={feedback} q={question[:40]}",
        ip,
        request.headers.get("user-agent"),
    )

    if feedback == "good":
        return HTMLResponse(
            '<span class="text-xs text-green-400 flex items-center gap-1">'
            '<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>'
            "</svg> Merci ! Charlie retient cette réponse.</span>"
        )
    return HTMLResponse(
        '<span class="text-xs text-purple-400 flex items-center gap-1">'
        '<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>'
        "</svg> Correction enregistrée. Charlie apprend.</span>"
    )
