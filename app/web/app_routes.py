from __future__ import annotations

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app.web.deps import get_db, require_operator

log = structlog.get_logger()
router = APIRouter(prefix="/app", tags=["app"])
templates = Jinja2Templates(directory="app/web/templates")

_CATEGORIES = ["demande_client", "urgent", "newsletter", "facture", "spam", "phishing", "rappel", "autre"]
_STATUSES = ["pending", "approved", "rejected", "sent", "reviewed"]
_PRIORITIES = ["high", "normal", "low"]


async def _fetch_counts(db: aiosqlite.Connection, filters: dict) -> dict:
    base_where = "1=1"
    params = []
    for col in ("mailbox_name", "status", "priority"):
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


async def _fetch_mailboxes(db: aiosqlite.Connection) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT mailbox_name FROM mail_processed WHERE mailbox_name IS NOT NULL"
    ) as cursor:
        rows = await cursor.fetchall()
    return sorted([r[0] for r in rows])


async def _fetch_mail(db: aiosqlite.Connection, mail_id: int) -> dict | None:
    async with db.execute(
        "SELECT id, mailbox_name, subject, sender, received_at, category, "
        "status, priority, ai_draft, human_draft, reviewed_by, reviewed_at, "
        "sent_at, sent_by, body_preview "
        "FROM mail_processed WHERE id = ?",
        (mail_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    cols = [
        "id", "mailbox_name", "subject", "sender", "received_at", "category",
        "status", "priority", "ai_draft", "human_draft", "reviewed_by",
        "reviewed_at", "sent_at", "sent_by", "body_preview",
    ]
    return dict(zip(cols, row, strict=True))


async def _fetch_draft_versions(db: aiosqlite.Connection, mail_id: int) -> list[dict]:
    async with db.execute(
        "SELECT id, version, body, editor_id, ai_generated, created_at "
        "FROM draft_versions WHERE mail_processed_id = ? ORDER BY version DESC",
        (mail_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    cols = ["id", "version", "body", "editor_id", "ai_generated", "created_at"]
    return [dict(zip(cols, row, strict=True)) for row in rows]


@router.get("/")
async def app_index(
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

    mails = await _fetch_mails(db, box, category, status, priority, sort_col, sort_order)
    mailboxes = await _fetch_mailboxes(db)
    counts = await _fetch_counts(
        db,
        {
            "mailbox_name": box,
            "status": status,
            "priority": priority,
        },
    )

    return templates.TemplateResponse(
        request,
        "app/inbox.html",
        {
            "mails": mails,
            "filters": {
                "box": box, "category": category, "status": status,
                "priority": priority, "sort": sort_col, "order": sort_order,
            },
            "categories": _CATEGORIES,
            "mailboxes": mailboxes,
            "statuses": _STATUSES,
            "priorities": _PRIORITIES,
            "counts": counts,
            "user": user,
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
    return templates.TemplateResponse(
        request,
        "app/conversation.html",
        {
            "mail": mail,
            "versions": versions,
            "user": user,
        },
    )
