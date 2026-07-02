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
# v1.29.0.3 — expose la version courante à TOUS les templates automatiquement
templates.env.globals["app_version"] = __version__

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
    # v1.29.0 — LIMIT relevé 200 → 1000. Le grouping threads se fait sur
    # le résultat : si LIMIT tronque un fil (le parent est au-delà de
    # la limite), `_group_into_threads` produit un fil incomplet avec
    # reply_count=0 → l'inbox affiche le mail comme une ligne plate
    # au lieu d'1 parent + N replies enfilées. 1000 couvre largement
    # les 662 mails actuels + la croissance. Le proper fix (grouping
    # en SQL natif) est tracké en v1.29.1.
    limit: int = 1000,
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
    # v1.30.0.2 — TRI PRIORITAIRE INCONDITIONNEL pour Daniel.
    # Quel que soit le filtre (boîte, catégorie, statut, vue Fils/Brute/Doublons)
    # ou le tri choisi par l'utilisateur (date/sujet/etc.) : les demande_client
    # pending sont TOUJOURS en premier. C'est le flux de travail de Daniel — il
    # doit voir ce qu'il a à traiter avant tout le reste, jamais dispersés dans
    # la liste. Le tri utilisateur (col + order) s'applique en 2e niveau.
    # v1.30.0.3 — élargi : inclut urgent pending (catégorie intermédiaire entre
    # demande_client et le reste du backlog).
    priority_order = (
        "(CASE "
        "WHEN category = 'demande_client' AND priority = 'high' AND (status = 'pending' OR status IS NULL) THEN 0 "
        "WHEN category = 'demande_client' AND (status = 'pending' OR status IS NULL) THEN 1 "
        "WHEN category = 'urgent' AND (status = 'pending' OR status IS NULL) THEN 2 "
        "WHEN (status = 'pending' OR status IS NULL) THEN 3 "
        "ELSE 4 "
        "END)"
    )
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
        # v1.29.0.2 — `body` complet RETIRÉ de la projection inbox.
        # Il pesait 0.9 MB pour 305 rows (avg 3256 chars/row) alors qu'il
        # n'est utilisé NULLE PART dans la liste (juste dans la page
        # conversation qui refetch séparément via _fetch_conversation).
        # Gain perf : -90% payload SQL, -90% temps render Jinja2.
        # Le body_preview (~200 chars) reste pour la recherche full-text.
        "attachment_count",
        # v1.29.0.7 — `ai_draft` REMPLACÉ par `has_draft` (bool 0/1).
        # AVANT : on tirait le texte complet du brouillon (souvent 2-5 KB par mail)
        # pour 618 mails = ~3 MB de payload inutile. Seul l'inbox_rows.html s'en
        # sert via `m.ai_draft|length > 0` (juste pour savoir s'il EXISTE).
        # APRÈS : booléen calculé en SQL (IFNULL(LENGTH(ai_draft) > 0, 0)) —
        # le template fait `m.has_draft` (1/0 → true/false en Jinja).
        # Le brouillon complet reste chargé uniquement dans /app/conversation/{id}.
        "has_draft",
        "suggested_subject",
        # v1.29.0.4 — `thread_id` AJOUTÉ à la projection.
        # AVANT : absent du SELECT → `dict(zip(cols, row))` décalait tout
        # d'1 cran → tous les mails finissaient en `orphans` (1 ligne = 1 fil).
        # Le threading était inopérant visuellement alors que la DB était OK.
        # MAINTENANT : 16 colonnes dans SELECT = 16 colonnes attendues par cols.
        "thread_id",
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

    # ── Requête 1 : HOT (demande_client OU urgent, toutes priorités, pending) ──
    # v1.30.0.3 — élargi : inclut TOUS les demande_client pending (pas seulement high)
    # + tous les urgent pending. C'est le backlog de travail de Daniel.
    # v1.30.0.4 — garde-fou anti-bruit dans la hot band.
    # Le tri SQL est correct (catégorie=demande_client + status=pending) MAIS la
    # classification en amont est trop large et met dans la hot :
    #  - des newsletters (Pluxee Card, Reçu Apple) classées demande_client à tort ;
    #  - des mails administratifs du comptable (cvfconsult.be) : bilans, versements ;
    #  - des mails internes du cabinet (cdal@digitalhs.biz) qui passent par préfiltre
    #    mais dont le backfill v1.28.2 n'a pas reclassifié les anciens.
    # On applique un filtre déterministe "manifestement pas un client" pour exclure
    # ces lignes de la hot band, SANS toucher à la classification en DB
    # (réversible et sans effet de bord sur le préfiltre upstream).
    # Le filtre est conservateur : on n'exclut que les expéditeurs/sujets INCONTESTABLEMENT
    # non-client. Une vraie demande client mal orthographiée passe toujours.
    hot_exclude_sender_patterns = [
        "%@digitalhs.biz",  # CDAL + staff cabinet (is_internal_sender via prefilter,
        #                    mais backfill incomplet sur les anciens mails)
        "%@cvfconsult.be",  # Comptable externe (bilan, versement, NCAE…)
    ]
    hot_exclude_subject_patterns = [
        "%pluxee%",         # Newsletter Pluxee Card
        "%reçu apple%",     # Newsletter Apple Store
        "%recu apple%",     # Variante sans accent
        "%e-box%",          # e-Box sécurité sociale
    ]
    hot_exclude_clauses = []
    for pat in hot_exclude_sender_patterns:
        hot_exclude_clauses.append(f"LOWER(IFNULL(m.sender, '')) NOT LIKE ?")
    for pat in hot_exclude_subject_patterns:
        hot_exclude_clauses.append(f"LOWER(IFNULL(m.subject, '')) NOT LIKE ?")
    hot_where = where + [
        "(category = 'demande_client' OR category = 'urgent')",
        "(status = 'pending' OR status IS NULL)",
    ] + hot_exclude_clauses
    hot_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count, "
        "CASE WHEN IFNULL(LENGTH(m.ai_draft), 0) > 0 THEN 1 ELSE 0 END AS has_draft, "
        "m.suggested_subject, m.thread_id "
        "FROM mail_processed m WHERE " + " AND ".join(hot_where) + " "
        f"ORDER BY {priority_order}, {col} {order} LIMIT ?"
    )
    hot_params = params.copy()
    # v1.30.0.4 — params des filtres anti-bruit de la hot (mêmes valeurs
    # en lowercase pour matcher les `LOWER(...) NOT LIKE ?` du WHERE).
    for pat in hot_exclude_sender_patterns + hot_exclude_subject_patterns:
        hot_params.append(pat.lower())
    hot_params.append(limit)
    async with db.execute(hot_sql, hot_params) as cursor:
        hot_rows = await cursor.fetchall()
    hot_mails = [_mask_sender(dict(zip(cols, row, strict=True))) for row in hot_rows]

    # ── Requête 2 : OTHER (tout sauf hot) ──
    # v1.30.0.4 — la définition de "hot" inclut maintenant les filtres anti-bruit
    # (sender @digitalhs.biz, @cvfconsult.be, sujets Pluxee/Apple/e-Box). L'other
    # doit être l'inverse exact de la hot : `NOT (hot_where)` avec les mêmes params.
    # On reconstruit l'expression NOT autour de toute la conjonction de hot_where.
    # NOTE : `where` (cutoff + filtres user) est déjà DANS hot_where, donc on
    # ne le remet pas dans other_where (sinon doublon du placeholder cutoff).
    hot_where_expr = "(" + " AND ".join(hot_where) + ")"
    other_where = [f"NOT {hot_where_expr}"]
    other_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count, "
        "CASE WHEN IFNULL(LENGTH(m.ai_draft), 0) > 0 THEN 1 ELSE 0 END AS has_draft, "
        "m.suggested_subject, m.thread_id "
        "FROM mail_processed m WHERE " + " AND ".join(other_where) + " "
        f"ORDER BY {priority_order}, {col} {order} LIMIT ?"
    )
    other_params = params.copy()
    # v1.30.0.4 — other_params doit avoir les MÊMES params que hot_params puisque
    # other_where = NOT(hot_where) et hot_where inclut déjà le cutoff+filtres user.
    for pat in hot_exclude_sender_patterns + hot_exclude_subject_patterns:
        other_params.append(pat.lower())
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

    # v1.29.1.5 — si le parent d'un fil a un status non-pending (approved/rejected/sent/reviewed),
    # c'est qu'il a déjà été traité par Daniel. Le grouper avec une reply pending n'apporte
    # aucune valeur visuelle (Daniel ne peut plus rien faire sur le parent). On ÉCLATE le fil
    # en orphelins pour que chaque mail pending s'affiche comme un mail simple (sans `›`,
    # sans bordure purple) — c'est cohérent avec la sémantique "1 mail pending = 1 ligne".
    # Cas concret : le bucket `adhoc::unknown::50d8b4a9` regroupe 207 mails sans rapport
    # (Infomaniak, Coolblue, formations, etc.) sous un parent "Un message du fondateur
    # d'Infomaniak" déjà approved. Éclater le fil règle le bug "reply sans parent visible".
    final_threads: list[dict] = []
    for t in list(threads_dict.values()) + orphans:
        parent_status = (t["parent"].get("status") or "").lower()
        if parent_status and parent_status != "pending":
            # Parent déjà traité → on convertit toutes les replies en orphelins
            for r in t["replies"]:
                final_threads.append(
                    {
                        "thread_id": f"orphan::{r['id']}",
                        "parent": r,
                        "replies": [],
                        "reply_count": 0,
                        "last_received": r.get("received_at") or r.get("processed_at") or "",
                        "all_duplicate": r.get("status") == "duplicate",
                    }
                )
            # Le parent lui-même : s'il est dans le scope pending il reste orphelin,
            # sinon il n'est pas dans la liste d'origine (déjà filtré par _fetch_mails).
        else:
            final_threads.append(t)
    threads = final_threads

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
