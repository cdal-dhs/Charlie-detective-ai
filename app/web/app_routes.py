from __future__ import annotations

from pathlib import Path

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import MailboxConfig, get_settings
from app.pipeline.subject_fixer import mask_forwarder_sender
from app.web.deps import get_db, require_operator

log = structlog.get_logger()
router = APIRouter(prefix="/app", tags=["app"])
templates = Jinja2Templates(directory="app/web/templates")

# Masquer les mails traités avant le 20/05/2026 (démarrage propre du poller)
_CUTOFF_DATE = "2026-05-20"

_CATEGORIES = [
    "demande_client",
    "urgent",
    "newsletter",
    "facture",
    "spam",
    "phishing",
    "rappel",
    "autre",
]
_STATUSES = ["pending", "approved", "rejected", "sent", "reviewed"]
_PRIORITIES = ["high", "normal", "low"]


async def _fetch_counts(db: aiosqlite.Connection, filters: dict) -> dict:
    base_where = "processed_at >= ?"
    params = [_CUTOFF_DATE]
    mailbox_names = filters.get("mailbox_names")
    if mailbox_names is not None:
        if mailbox_names:
            placeholders = ",".join("?" for _ in mailbox_names)
            base_where += f" AND mailbox_name IN ({placeholders})"
            params.extend(mailbox_names)
        else:
            base_where += " AND 1=0"
    for col in ("status", "priority"):
        if filters.get(col):
            base_where += f" AND {col} = ?"
            params.append(filters[col])

    counts = {}
    for cat in _CATEGORIES:
        async with db.execute(
            f"SELECT COUNT(*) FROM mail_processed WHERE category = ? AND {base_where}",
            (cat, *params),
        ) as cursor:
            row = await cursor.fetchone()
            counts[cat] = row[0] if row else 0

    # Count urgent (high priority)
    urgent_where = "processed_at >= ?"
    urgent_params = [_CUTOFF_DATE]
    if mailbox_names is not None:
        if mailbox_names:
            placeholders = ",".join("?" for _ in mailbox_names)
            urgent_where += f" AND mailbox_name IN ({placeholders})"
            urgent_params.extend(mailbox_names)
        else:
            urgent_where += " AND 1=0"
    if filters.get("status"):
        urgent_where += " AND status = ?"
        urgent_params.append(filters["status"])
    async with db.execute(
        f"SELECT COUNT(*) FROM mail_processed WHERE priority = 'high' AND {urgent_where}",
        urgent_params,
    ) as cursor:
        row = await cursor.fetchone()
        counts["urgent_prio"] = row[0] if row else 0

    return counts


_SORTABLE_COLS = {
    "mailbox": "mailbox_name",
    "subject": "subject",
    "sender": "sender",
    "category": "category",
    "status": "status",
    "priority": "priority",
    "date": "processed_at",
}


async def _fetch_mails(
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
    """Retourne (hot_mails, other_mails).

    hot_mails = demande_client + high + pending (toujours en haut).
    other_mails = le reste, avec le même tri intelligent.
    """
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
        "body",
        "attachment_count",
        "ai_draft",
        "suggested_subject",
    ]

    def _mask_sender(row_dict: dict) -> dict:
        """Affiche NO_EMAIL_IN_THE_FORM pour les forwarders WP sans email client."""
        row_dict["sender"] = mask_forwarder_sender(
            row_dict.get("sender", ""), row_dict.get("body", "")
        )
        # v1.25.28 — sujet lisible du brouillon prioritaire sur le sujet original
        # (template WP absurde / tag [NO_EMAIL_IN_THE_FORM]). Cf. #643.
        if row_dict.get("suggested_subject"):
            row_dict["subject"] = row_dict["suggested_subject"]
        return row_dict

    # ── Requête 1 : HOT (demande_client + high + pending) ──
    hot_where = where + [
        "category = 'demande_client'",
        "priority = 'high'",
        "(status = 'pending' OR status IS NULL)",
    ]
    hot_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, m.body, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count, "
        "ai_draft, m.suggested_subject "
        "FROM mail_processed m WHERE " + " AND ".join(hot_where) + " "
        f"ORDER BY {col} {order} LIMIT ?"
    )
    hot_params = params.copy()
    hot_params.append(limit)
    async with db.execute(hot_sql, hot_params) as cursor:
        hot_rows = await cursor.fetchall()
    hot_mails = [_mask_sender(dict(zip(cols, row, strict=True))) for row in hot_rows]

    # ── Requête 2 : OTHER (tout sauf hot) ──
    other_where = where + [
        "NOT (category = 'demande_client' AND priority = 'high' AND (status = 'pending' OR status IS NULL))"
    ]
    other_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, m.body, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count, "
        "ai_draft, m.suggested_subject "
        "FROM mail_processed m WHERE " + " AND ".join(other_where) + " "
        f"ORDER BY (m.status = 'pending') DESC, (m.priority = 'high') DESC, {col} {order} LIMIT ?"
    )
    other_params = params.copy()
    other_params.append(limit)
    async with db.execute(other_sql, other_params) as cursor:
        other_rows = await cursor.fetchall()
    other_mails = [_mask_sender(dict(zip(cols, row, strict=True))) for row in other_rows]

    return hot_mails, other_mails


def _group_into_threads(mails: list[dict]) -> list[dict]:
    """v1.29.0 — groupe les mails en fils de discussion par thread_id.

    Args:
        mails: liste de dicts (issus de _fetch_mails).

    Returns:
        Liste de threads triés par date du mail le plus récent DESC.
        Chaque thread = {
            "thread_id": str,
            "parent": dict (mail avec received_at min),
            "replies": [dict, ...] (du + récent au + ancien),
            "reply_count": int,
            "last_received": str (ISO du mail le + récent),
            "all_duplicate": bool (tous les mails du fil sont status=duplicate)
        }

    Les mails sans thread_id (anciens, pré-v1.29.0) restent en 1-mail = 1-fil
    (le grouping est best-effort, pas destructif).
    """
    threads_dict: dict[str, dict] = {}
    orphans: list[dict] = []

    for mail in mails:
        tid = mail.get("thread_id") or ""
        if not tid:
            # Mail pré-v1.29.0 ou pas de thread — orphelin, traité comme 1 fil.
            orphans.append(
                {
                    "thread_id": f"orphan::{mail['id']}",
                    "parent": mail,
                    "replies": [],
                    "reply_count": 0,
                    "last_received": mail.get("received_at") or mail.get("processed_at") or "",
                    "all_duplicate": mail.get("status") == "duplicate",
                }
            )
            continue
        if tid not in threads_dict:
            threads_dict[tid] = {
                "thread_id": tid,
                "parent": mail,
                "replies": [],
                "reply_count": 0,
                "last_received": mail.get("received_at") or mail.get("processed_at") or "",
                "all_duplicate": True,
            }
        else:
            t = threads_dict[tid]
            # Met à jour le parent si ce mail est plus ancien
            cur_parent_dt = t["parent"].get("received_at") or ""
            mail_dt = mail.get("received_at") or ""
            if mail_dt < cur_parent_dt:
                old_parent = t["parent"]
                t["parent"] = mail
                t["replies"].append(old_parent)
            else:
                t["replies"].append(mail)
            # last_received
            if (mail.get("received_at") or "") > t["last_received"]:
                t["last_received"] = mail.get("received_at") or t["last_received"]
        if mail.get("status") != "duplicate":
            threads_dict[tid]["all_duplicate"] = False

    # Calcul reply_count
    for t in threads_dict.values():
        t["reply_count"] = len(t["replies"])
        # Tri replies du + récent au + ancien
        t["replies"].sort(
            key=lambda m: m.get("received_at") or "", reverse=True
        )

    threads = list(threads_dict.values()) + orphans
    # Tri global par date du mail le plus récent DESC
    threads.sort(key=lambda t: t["last_received"], reverse=True)
    return threads


async def _fetch_mailboxes() -> list[MailboxConfig]:
    """Retourne les boîtes configurées (pas seulement celles avec mails)."""
    settings = get_settings()
    return settings.mailboxes()


async def _fetch_mail(db: aiosqlite.Connection, mail_id: int) -> dict | None:
    async with db.execute(
        "SELECT id, mailbox_name, subject, sender, received_at, category, "
        "status, priority, ai_draft, human_draft, reviewed_by, reviewed_at, "
        "sent_at, sent_by, body_preview, body, suggested_subject "
        "FROM mail_processed WHERE id = ?",
        (mail_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    cols = [
        "id",
        "mailbox_name",
        "subject",
        "sender",
        "received_at",
        "category",
        "status",
        "priority",
        "ai_draft",
        "human_draft",
        "reviewed_by",
        "reviewed_at",
        "sent_at",
        "sent_by",
        "body_preview",
        "body",
        "suggested_subject",
    ]
    mail = dict(zip(cols, row, strict=True))
    # v1.25.18 — affiche NO_EMAIL_IN_THE_FORM pour les forwarders WP sans email client.
    mail["sender"] = mask_forwarder_sender(mail.get("sender", ""), mail.get("body", ""))
    # v1.25.28 — sujet lisible du brouillon prioritaire sur le sujet original. Cf. #643.
    if mail.get("suggested_subject"):
        mail["subject"] = mail["suggested_subject"]
    return mail


async def _fetch_attachments(db: aiosqlite.Connection, mail_id: int) -> list[dict]:
    async with db.execute(
        "SELECT id, filename, storage_path, size_bytes, extracted_text_preview, created_at "
        "FROM email_attachment WHERE mail_processed_id = ? ORDER BY id",
        (mail_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    cols = ["id", "filename", "storage_path", "size_bytes", "extracted_text_preview", "created_at"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


async def _fetch_draft_versions(db: aiosqlite.Connection, mail_id: int) -> list[dict]:
    async with db.execute(
        "SELECT id, version, body, editor_id, ai_generated, created_at "
        "FROM draft_versions WHERE mail_processed_id = ? ORDER BY version DESC",
        (mail_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    cols = ["id", "version", "body", "editor_id", "ai_generated", "created_at"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


@router.get("/inbox")
async def app_inbox_redirect(request: Request) -> RedirectResponse:
    """Redirect /app/inbox → /app/ pour éviter les 404."""
    qp = "?" + request.query_params if request.query_params else ""
    return RedirectResponse(url=f"/app/{qp}", status_code=302)


@router.get("/")
async def app_index(
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
    # v1.29.0 — view tabs cockpit : threads (défaut) / flat / duplicates
    view = request.query_params.get("view") or "threads"

    hot_mails, other_mails = await _fetch_mails(
        db, boxes, category, status, priority, q, sort_col, sort_order
    )

    # v1.29.0 — vue par défaut = threads (regroupés par thread_id).
    # Vue flat = 1 ligne = 1 mail (legacy). Vue duplicates = uniquement
    # les status='duplicate' (audit/debug v1.28.3).
    if view == "threads":
        hot_threads = _group_into_threads(hot_mails)
        other_threads = _group_into_threads(other_mails)
    elif view == "duplicates":
        # Filtre uniquement les doublons (status='duplicate') sur le tri descendant
        hot_threads = [
            {
                "thread_id": f"dup::{m['id']}",
                "parent": m,
                "replies": [],
                "reply_count": 0,
                "last_received": m.get("received_at") or m.get("processed_at") or "",
                "all_duplicate": True,
            }
            for m in hot_mails
            if m.get("status") == "duplicate"
        ]
        other_threads = [
            {
                "thread_id": f"dup::{m['id']}",
                "parent": m,
                "replies": [],
                "reply_count": 0,
                "last_received": m.get("received_at") or m.get("processed_at") or "",
                "all_duplicate": True,
            }
            for m in other_mails
            if m.get("status") == "duplicate"
        ]
    else:
        # view == "flat" — comportement legacy, 1 ligne = 1 mail
        hot_threads = [
            {
                "thread_id": f"flat::{m['id']}",
                "parent": m,
                "replies": [],
                "reply_count": 0,
                "last_received": m.get("received_at") or m.get("processed_at") or "",
                "all_duplicate": m.get("status") == "duplicate",
            }
            for m in hot_mails
        ]
        other_threads = [
            {
                "thread_id": f"flat::{m['id']}",
                "parent": m,
                "replies": [],
                "reply_count": 0,
                "last_received": m.get("received_at") or m.get("processed_at") or "",
                "all_duplicate": m.get("status") == "duplicate",
            }
            for m in other_mails
        ]

    mailboxes = await _fetch_mailboxes()
    counts = await _fetch_counts(
        db,
        {
            "mailbox_names": boxes,
            "status": status,
            "priority": priority,
        },
    )

    return templates.TemplateResponse(
        request,
        "app/inbox.html",
        {
            "hot_threads": hot_threads,
            "other_threads": other_threads,
            "view": view,
            "filters": {
                "box": box_raw,
                "category": category,
                "status": status,
                "priority": priority,
                "q": q,
                "sort": sort_col,
                "order": sort_order,
            },
            "categories": _CATEGORIES,
            "mailboxes": mailboxes,
            "box_short": {mb.name: mb.short_code for mb in mailboxes},
            "statuses": _STATUSES,
            "priorities": _PRIORITIES,
            "counts": counts,
            "user": user,
            "version": __version__,
        },
    )


@router.get("/conversation/{mail_id}")
async def conversation_page(
    request: Request,
    mail_id: int,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_operator),  # noqa: B008
):
    mail = await _fetch_mail(db, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="Mail not found")

    versions = await _fetch_draft_versions(db, mail_id)
    attachments = await _fetch_attachments(db, mail_id)
    return templates.TemplateResponse(
        request,
        "app/conversation.html",
        {
            "mail": mail,
            "versions": versions,
            "attachments": attachments,
            "user": user,
            "version": __version__,
        },
    )


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    request: Request,
    attachment_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    user: dict = Depends(require_operator),
):
    async with db.execute(
        "SELECT storage_path, filename FROM email_attachment WHERE id = ?",
        (attachment_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pièce jointe introuvable en base")
    storage_path, filename = row
    settings = get_settings()
    path = Path(storage_path)
    # Support anciens chemins absolus (pré-v1.12.5) + nouveaux relatifs
    if not path.is_absolute():
        path = settings.data_dir / path
    if not path.exists():
        log.warning(
            "attachment.file_missing",
            attachment_id=attachment_id,
            storage_path=storage_path,
            resolved_path=str(path),
            data_dir=str(settings.data_dir),
        )
        raise HTTPException(
            status_code=404,
            detail=f"Fichier non disponible sur le disque (supprimé ou migration perdue). Path attendu : {path}",
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )
