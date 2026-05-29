from __future__ import annotations

from pathlib import Path

import aiosqlite
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import asyncio
from datetime import datetime

from app.cerveau_client import feed_document, get_backup_status
from app.config import get_settings
from app.delivery.slack_notifier import send_slack_message
from app.healthcheck import health
from app.pipeline.document_extract import extract_text_bytes, is_supported
from app.web.deps import get_db, require_admin
from app.web.utils import FernetManager, audit_log

log = structlog.get_logger()
router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/web/templates")

_SETTINGS_KEYS = [
    "llm_provider",
    "llm_base_url",
    "llm_api_key_encrypted",
    "llm_model_default",
    "llm_model_classifier",
    "llm_model_draft",
    "llm_model_fallback",
    "llm_temperature_analysis",
    "llm_temperature_draft",
    "llm_max_tokens",
]


async def _stats(db: aiosqlite.Connection) -> dict:
    today_sql = "date(processed_at) = date('now')"
    cutoff = "processed_at >= '2026-05-20'"
    stats = {}
    for key, sql in [
        ("total_today", f"SELECT COUNT(*) FROM mail_processed WHERE {today_sql} AND {cutoff}"),
        ("pending", f"SELECT COUNT(*) FROM mail_processed WHERE status = 'pending' AND {cutoff}"),
        ("approved", f"SELECT COUNT(*) FROM mail_processed WHERE status = 'approved' AND {cutoff}"),
        ("rejected", f"SELECT COUNT(*) FROM mail_processed WHERE status = 'rejected' AND {cutoff}"),
        ("sent", f"SELECT COUNT(*) FROM mail_processed WHERE status = 'sent' AND {cutoff}"),
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


async def _recent_telemetry(db: aiosqlite.Connection, limit: int = 10) -> list[dict]:
    async with db.execute(
        "SELECT id, event_type, mailbox_name, details, created_at "
        "FROM agent_telemetry ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    cols = ["id", "event_type", "mailbox_name", "details", "created_at"]
    return [dict(zip(cols, r, strict=True)) for r in rows]


async def _load_settings(db: aiosqlite.Connection, env_settings: dict | None = None) -> dict:
    """Charge les paramètres DB avec fallback sur les valeurs .env."""
    db_vals = {}
    async with db.execute("SELECT key, value, is_encrypted FROM app_settings") as cur:
        async for row in cur:
            key, value, is_enc = row
            db_vals[key] = {"value": value or "", "masked": bool(is_enc)}
    # Ensure all known keys exist with .env fallback
    for key in _SETTINGS_KEYS:
        if key not in db_vals or not db_vals[key]["value"]:
            # Fallback sur la valeur .env correspondante
            fallback = ""
            if env_settings and key in env_settings:
                fallback = env_settings[key]
            db_vals[key] = {"value": fallback, "masked": "key" in key.lower() or "token" in key.lower()}
    return db_vals


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
    telemetry = await _recent_telemetry(db, 10)
    snap = health.snapshot()
    db_size = Path(get_settings().db_agent_state).stat().st_size
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "stats": stats,
            "audit": audit,
            "telemetry": telemetry,
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
    env = get_settings()
    env_settings = {
        "llm_model_default": env.llm_model_default,
        "llm_model_classifier": env.llm_model_classifier,
        "llm_model_fallback": env.llm_model_fallback,
    }
    settings = await _load_settings(db, env_settings)
    env_full = {
        "imap_host": env.imap_host,
        "imap_port": env.imap_port,
        "ollama_pro_base_url": env.ollama_pro_base_url,
        "llm_model_default": env.llm_model_default,
        "llm_model_classifier": env.llm_model_classifier,
        "llm_model_fallback": env.llm_model_fallback,
        "resend_from": env.resend_from,
        "draft_recipient": env.draft_recipient,
        "slack_webhook_url": env.slack_webhook_url,
        "poll_interval_seconds": env.poll_interval_seconds,
        "embedding_model": env.embedding_model,
        "rag_top_k": env.rag_top_k,
        "mailboxes": [
            {
                "id": i + 1,
                "name": mb.name,
                "user": mb.user,
                "brand": mb.brand,
                "default_lang": mb.default_lang,
            }
            for i, mb in enumerate(env.mailboxes())
        ],
    }
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"settings": settings, "cfg": env_full, "user": user},
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
        raw = form.get(key)
        if raw is None:
            continue
        raw = str(raw).strip()
        is_enc = "key" in key.lower() or "token" in key.lower()
        if not raw:
            # Champ vidé → suppression pour fallback .env
            await db.execute(
                "DELETE FROM app_settings WHERE key = ?",
                (key,),
            )
            continue
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
        f'<div class="p-2 bg-{status}-900/40 border border-{status}-800 '
        f'rounded text-{status}-300 text-sm">{msg}</div>'
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
        f'<div class="p-2 bg-{status}-900/40 border border-{status}-800 '
        f'rounded text-{status}-300 text-sm">{msg}</div>'
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


@router.get("/api/soul")
async def soul_read(
    user: dict = Depends(require_admin),  # noqa: B008
):
    """Retourne le contenu actuel de SOUL.md."""
    soul_path = get_settings().data_dir / "SOUL.md"
    content = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    return {"content": content}


@router.post("/api/soul")
async def soul_save(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    """Sauvegarde le contenu de SOUL.md."""
    body = await request.json()
    content = str(body.get("content", ""))
    soul_path = get_settings().data_dir / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(content, encoding="utf-8")

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "soul_update", "prompt", "SOUL.md",
        None, ip, request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.get("/documents")
async def documents_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    """Page upload et gestion des documents."""
    # Historique local des uploads récents
    async with db.execute(
        "SELECT doc_id, dossier_id, marque, titre, format, type, date, created_at "
        "FROM document_scanned ORDER BY created_at DESC LIMIT 50"
    ) as cur:
        rows = await cur.fetchall()
    cols = ["doc_id", "dossier_id", "marque", "titre", "format", "type", "date", "created_at"]
    documents = [dict(zip(cols, r, strict=True)) for r in rows]
    return templates.TemplateResponse(
        request,
        "admin/documents.html",
        {"documents": documents, "user": user},
    )


@router.post("/api/documents/upload")
async def documents_upload(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),  # noqa: B008
    user: dict = Depends(require_admin),  # noqa: B008
):
    """Reçoit un fichier, extrait le texte, et l'ingère dans Cerveau2."""
    settings = get_settings()
    form = await request.form()

    file = form.get("file")
    if not file or not getattr(file, "filename", None):
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded text-red-300 text-sm">'
            "Aucun fichier sélectionné."
            "</div>"
        )

    filename = str(file.filename)
    if not is_supported(filename):
        return HTMLResponse(
            '<div class="p-3 bg-yellow-900/40 border border-yellow-800 rounded text-yellow-300 text-sm">'
            f"Format non supporté : {filename}. "
            "Extensions acceptées : txt, md, csv, json, xml, html, pdf, docx, jpg, png, tiff."
            "</div>"
        )

    dossier_id = str(form.get("dossier_id", "")).strip().upper()
    marque = str(form.get("marque", "detectivebelgique")).strip()
    titre = str(form.get("titre", filename)).strip() or filename

    try:
        content = await file.read()
        text = extract_text_bytes(content, filename)
        if not text or not text.strip():
            return HTMLResponse(
                '<div class="p-3 bg-yellow-900/40 border border-yellow-800 rounded text-yellow-300 text-sm">'
                f"Fichier vide ou texte non extractible : {filename}."
                "</div>"
            )
    except Exception as e:
        log.warning("admin.upload_extraction_failed", filename=filename, error=str(e))
        return HTMLResponse(
            '<div class="p-3 bg-red-900/40 border border-red-800 rounded text-red-300 text-sm">'
            f"Erreur extraction : {filename}."
            "</div>"
        )

    doc_id = f"doc-upload-{hash(filename + dossier_id + marque) % 100000000:08d}"
    date_str = datetime.now().strftime("%Y-%m-%d")

    # Persist local tracking
    await db.execute(
        "INSERT INTO document_scanned (doc_id, dossier_id, marque, titre, format, type, date, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET created_at=datetime('now')",
        (doc_id, dossier_id, marque, titre, Path(filename).suffix.lower().lstrip("."), "document", date_str, len(content)),
    )
    await db.commit()

    # Fire-and-forget Cerveau2
    asyncio.create_task(  # noqa: RUF006
        feed_document(
            doc_id=doc_id,
            type="document",
            dossier_id=dossier_id,
            marque=marque,
            date=date_str,
            titre=titre,
            body=text,
            metadata={"source": "cockpit_upload", "filename": filename, "size_bytes": len(content)},
            base_url=settings.cerveau2_base_url,
            api_secret=settings.cerveau2_api_secret,
        )
    )

    ip = request.client.host if request.client else None
    await audit_log(
        db, user["id"], "document_upload", "document", doc_id,
        f"{filename} -> {dossier_id}", ip, request.headers.get("user-agent"),
    )

    return HTMLResponse(
        '<div class="p-3 bg-green-900/40 border border-green-800 rounded text-green-300 text-sm">'
        f"<b>{filename}</b> ingéré avec succès dans Cerveau2 "
        f"(dossier <b>{dossier_id}</b>, marque <b>{marque}</b>)."
        "</div>"
    )


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
    cols = [
        "id", "user_id", "action", "resource_type",
        "resource_id", "details", "ip_address", "created_at",
    ]
    logs = [dict(zip(cols, r, strict=True)) for r in rows]

    total_pages = (total + per_page - 1) // per_page

    # Récupérer statut backup Cerveau2 (rassurance opérationnelle)
    settings = get_settings()
    backup_info = await get_backup_status(
        base_url=settings.cerveau2_base_url,
        api_secret=settings.cerveau2_api_secret,
    )

    # Télémétrie poller : cycles des dernières 24h
    telemetry_sql = (
        "SELECT event_type, mailbox_name, details, created_at "
        "FROM agent_telemetry WHERE created_at >= datetime('now', '-1 day') "
        "ORDER BY created_at DESC LIMIT 30"
    )
    async with db.execute(telemetry_sql) as cur:
        t_rows = await cur.fetchall()
    telemetry = [
        dict(zip(["event_type", "mailbox_name", "details", "created_at"], r, strict=True))
        for r in t_rows
    ]

    # Emails traités aujourd'hui (résumé)
    today_mails_sql = (
        "SELECT mailbox_name, category, COUNT(*) as cnt "
        "FROM mail_processed WHERE date(processed_at) = date('now') "
        "GROUP BY mailbox_name, category ORDER BY cnt DESC"
    )
    async with db.execute(today_mails_sql) as cur:
        m_rows = await cur.fetchall()
    today_mails = [
        dict(zip(["mailbox_name", "category", "count"], r, strict=True))
        for r in m_rows
    ]

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
            "cerveau2_backup": backup_info,
            "telemetry": telemetry,
            "today_mails": today_mails,
        },
    )
