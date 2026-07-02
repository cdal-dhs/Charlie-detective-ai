from __future__ import annotations

import re
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

    hot_mails = demande_client + urgent + pending (toujours en haut).
    other_mails = tout le reste (toutes catégories, statuts, replies, doublons,
    traités, etc.) — rien n'est masqué, tout est visible.
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
    # v1.31.0 — RESTRICTION hot_where à priority='high' (URGENT seulement).
    # v1.30.0.x laissait passer les priority='normal' (donc "Re: Pour devis" en normal
    # apparaissait en hot band verte). Le user veut STRICTEMENT les high en vert.
    # Les priority='normal' pending vont en other band (où ils sont toujours
    # triés en tête par priority_order SQL, juste pas en cadre vert).
    base_where = base_where + [
        "priority = 'high'",
    ]
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
        # v1.30.0.8 — `in_reply_to` et `message_id` AJOUTÉS à la projection.
        # Utilisés par `_group_into_threads()` pour détecter les "orphelins-replies"
        # (mail avec in_reply_to pointant vers un message_id absent de notre DB)
        # et s'assurer qu'un tel mail ne soit JAMAIS promu parent d'un fil.
        # 2 colonnes supplémentaires → 18 colonnes au total dans SELECT.
        "in_reply_to",
        "message_id",
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
    # v1.30.0.13 — un mail dont le sujet commence par Re:/Re :/Re\xa0: (et
    # variantes AW:/TR:/Fwd:) est une réponse. Son parent n'est pas dans
    # la hot band (sinon le grouping l'aurait rattaché au fil parent) :
    # c'est forcément un reply orphelin cross-band. Le sortir de la hot
    # band pour que Daniel ne voie JAMAIS de "Re: ..." en première ligne
    # verte (cf. demande CDAL — l'orphelin doit aller dans la other band).
    # v1.30.0.5/v1.30.0.12 essayaient de le faire dans `_group_into_threads`
    # mais le mail était DÉJÀ dans `hot_mails` à ce stade → déplacer après
    # groupement ne résolvait rien. Le fix correct = exclure dès la requête
    # SQL, AVANT le grouping.
    hot_exclude_reply_prefix_patterns = [
        "re:%",       # Re:Demande, Re: facture…
        "re :%",      # Re : avec espace
        "re\xa0:%",   # Re\xa0: avec espace insécable (NBSP, frequent sur Mac)
        "aw:%",       # AW: Antwort
        "tr:%",       # TR: Transfert/Réponse (Apple Mail)
        "fwd:%",      # Fwd: Forward
        "fw:%",       # Fw: Forward court
        "sv:%",       # SV: Sent/Reply (Outlook)
    ]
    for pat in hot_exclude_reply_prefix_patterns:
        hot_exclude_clauses.append(f"LOWER(IFNULL(m.subject, '')) NOT LIKE ?")
    hot_where = where + [
        "(category = 'demande_client' OR category = 'urgent')",
        "(status = 'pending' OR status IS NULL)",
        "priority = 'high'",  # v1.31.0 — HOT = STRICTEMENT high priority
    ] + hot_exclude_clauses
    hot_sql = (
        "SELECT m.id, m.mailbox_name, m.subject, m.sender, m.received_at, m.category, "
        "m.status, m.priority, m.processed_at, m.body_preview, "
        "(SELECT COUNT(*) FROM email_attachment WHERE mail_processed_id = m.id) AS attachment_count, "
        "CASE WHEN IFNULL(LENGTH(m.ai_draft), 0) > 0 THEN 1 ELSE 0 END AS has_draft, "
        "m.suggested_subject, m.thread_id, "
        # v1.30.0.8 — colonnes threading brutes pour détecter les replies orphelins
        # (mail avec in_reply_to pointant vers un message_id absent du système).
        "m.in_reply_to, m.message_id "
        "FROM mail_processed m WHERE " + " AND ".join(hot_where) + " "
        f"ORDER BY {priority_order}, {col} {order} LIMIT ?"
    )
    hot_params = params.copy()
    # v1.30.0.4 — params des filtres anti-bruit de la hot (mêmes valeurs
    # en lowercase pour matcher les `LOWER(...) NOT LIKE ?` du WHERE).
    # v1.30.0.13 — inclut aussi les préfixes Re:/Re :/Re\xa0:/AW:/TR:/Fwd:
    # pour exclure les replies orphelins de la hot band dès la requête SQL.
    for pat in hot_exclude_sender_patterns + hot_exclude_subject_patterns + hot_exclude_reply_prefix_patterns:
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
        "m.suggested_subject, m.thread_id, "
        "m.in_reply_to, m.message_id "
        "FROM mail_processed m WHERE " + " AND ".join(other_where) + " "
        f"ORDER BY {priority_order}, {col} {order} LIMIT ?"
    )
    other_params = params.copy()
    # v1.30.0.4 — other_params doit avoir les MÊMES params que hot_params puisque
    # other_where = NOT(hot_where) et hot_where inclut déjà le cutoff+filtres user.
    # v1.30.0.13 — inclut aussi les préfixes Re:/Re :/Re\xa0: pour cohérence.
    for pat in hot_exclude_sender_patterns + hot_exclude_subject_patterns + hot_exclude_reply_prefix_patterns:
        other_params.append(pat.lower())
    other_params.append(limit)
    async with db.execute(other_sql, other_params) as cursor:
        other_rows = await cursor.fetchall()
    other_mails = [_mask_sender(dict(zip(cols, row, strict=True))) for row in other_rows]

    # v1.30.0.11 — rollback du mode worklist : on n'affiche plus UNIQUEMENT
    # la hot band dans "Toutes". Tout est visible (hot + other) pour donner
    # à Daniel une vision complète de sa boîte. Le tri `priority_order` garde
    # les demande_client pending en tête, mais aucune catégorie ni aucun statut
    # n'est masqué — c'est le comportement d'avant v1.30.0.7.
    return hot_mails, other_mails

# v1.30.0.5 — heuristique "ce mail ressemble à une réponse (reply)".
# Un mail pending dont le sujet commence par Re:/Re :/Re\xa0:/Re\xa0:/AW:/TR:/Fwd:
# est un reply dont le parent n'est PAS dans le set courant (sinon il aurait
# été groupé avec son parent). Cas concret : mail #677 (siimiya) — sujet
# "Re: DEMANDE D'Approbation..." mais parent #640 déjà approved → le parent
# n'est pas dans le hot set (filtré) → ce mail doit aller dans OTHER, pas HOT.
_REPLY_SUBJECT_PREFIX = re.compile(
    r"^\s*(re|aw|tr|fwd|sv|fw)\s*:\s*", re.IGNORECASE
)


def _looks_like_reply_subject(subject: str | None) -> bool:
    """True si le sujet commence par un préfixe de réponse (Re:/AW:/TR:/Fwd:).

    Le préfixe `Re:` (et ses variantes unicode avec espace insécable \xa0)
    est le marqueur universel d'un mail de réponse — Daniel ne doit pas voir
    un "Re: ..." en première ligne de la hot band car son parent est forcément
    ailleurs (déjà traité, hors-scope, ou étranger au thread groupable).
    """
    if not subject:
        return False
    return bool(_REPLY_SUBJECT_PREFIX.match(subject))


def _is_orphan_reply(mail: dict, known_message_ids: set[str], same_thread_message_ids: set[str]) -> bool:
    """v1.30.0.8 — détecte un mail qui est une réponse dont le parent est absent
    de NOTRE système (donc pas groupable avec un vrai premier mail).

    Un mail est un "reply orphelin" si :
    - `in_reply_to` est non-vide ET
    - le message_id référencé n'est NI dans `known_message_ids` (la DB entière)
      NI dans `same_thread_message_ids` (les autres mails du même thread).

    Conséquence : ce mail ne peut PAS être "parent" d'un fil — il appartient
    forcément à une conversation dont le premier mail n'est pas dans notre
    base. Le parent légitime du fil = NULL (ou, faute de mieux, ce mail
    lui-même, mais affiché SANS l'icône `›` — c'est le "premier mail connu").

    Cas concret : un client répond à un mail envoyé par Daniel il y a 6 mois.
    Le mail de Daniel n'est pas dans `mail_processed` (seuls les mails entrants
    y sont). Le reply entrant a un `in_reply_to` pointant vers le Message-ID
    de Daniel, qu'on ne trouve nulle part en DB → c'est un orphan reply.

    v1.30.0.12 — un mail avec un subject "Re:/AW:/TR:/Fwd: ..." est TOUJOURS
    considéré comme un reply (même si in_reply_to est NULL après ingestion
    IMAP). C'est un signal sémantique fort : le formatteur du mail a marqué
    ce mail comme une réponse, donc on le traite comme un reply orphelin
    par défaut. Conséquence : un fil où TOUS les mails ont un subject
    préfixé "Re:" → tous orphelins → parent promu sans icône ›.
    """
    # v1.30.0.12 — préfixe de réponse = signal sémantique fort
    if _looks_like_reply_subject(mail.get("subject")):
        return True
    in_reply_to = (mail.get("in_reply_to") or "").strip()
    if not in_reply_to:
        return False
    return in_reply_to not in known_message_ids and in_reply_to not in same_thread_message_ids


def _group_into_threads(
    mails: list[dict],
    all_thread_siblings: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """v1.29.0 — groupe les mails en fils de discussion par thread_id.

    Args:
        mails: liste de dicts (issus de _fetch_mails).
        all_thread_siblings: mails HORS du set `mails` mais qui partagent un
            thread_id avec un mail de `mails` (typiquement les siblings du
            other_mails set quand on groupe hot_mails). Permet de détecter
            un reply pending dont le parent est dans other (déjà traité)
            et de le DÉPLACER vers l'other band. Ignoré si None.

    Returns:
        Tuple (keep, move_to_other) :
        - keep : threads à garder dans la liste appelante (hot OU other).
        - move_to_other : entrées qui doivent DÉMÉNAGER dans l'autre band
          (typiquement un reply pending dont le parent est déjà traité).
        Chaque thread = {
            "thread_id": str,
            "parent": dict (mail avec received_at min OU premier mail non-orphelin),
            "replies": [dict, ...] (du + récent au + ancien),
            "reply_count": int,
            "last_received": str (ISO du mail le + récent),
            "all_duplicate": bool (tous les mails du fil sont status=duplicate),
            "parent_is_orphan": bool (le parent est lui-même un reply orphelin — pas d'icône ›),
            "all_orphans": bool (TOUS les mails du fil sont des replies orphelins)
        }

    Les mails sans thread_id (anciens, pré-v1.29.0) restent en 1-mail = 1-fil
    (le grouping est best-effort, pas destructif).

    v1.30.0.5 — la fonction retourne 2 listes. La logique de split a été
    enrichie : un reply pending dont le parent est non-pending (ou dont le
    sujet ressemble à un reply sans parent groupable) doit DÉMÉNAGER dans
    l'autre band — son parent est déjà traité par Daniel, il n'a plus rien
    à faire dans la hot band. Cf. CDAL "un sous mail ne peut pas être en
    premier il doit avoir un email parent !".

    v1.30.0.8 — un mail avec `in_reply_to` pointant vers un message_id absent
    de notre DB est un "reply orphelin" : son vrai parent n'est pas dans le
    système. Il NE DOIT PAS être promu parent d'un fil. Si le fil a AU MOINS
    UN mail non-orphelin, le parent = le plus ancien non-orphelin. Si TOUS les
    mails du fil sont des replies orphelins, le parent = le plus ancien, mais
    `parent_is_orphan=True` → rendu sans icône `›` ("premier mail connu" du fil,
    pas un vrai parent de conversation).
    """
    threads_dict: dict[str, dict] = {}
    orphans: list[dict] = []

    # v1.30.0.5 — index des siblings HORS `mails` (other_mails) par thread_id.
    # Utilisé pour détecter les replies pending dont le parent est dans other
    # (déjà traité par Daniel).
    siblings_by_tid: dict[str, list[dict]] = {}
    if all_thread_siblings:
        for sib in all_thread_siblings:
            sib_tid = sib.get("thread_id") or ""
            if sib_tid:
                siblings_by_tid.setdefault(sib_tid, []).append(sib)

    # v1.30.0.8 — index des message_id connus dans `mails` + `all_thread_siblings`.
    # Utilisé par `_is_orphan_reply()` pour décider si l'in_reply_to d'un mail
    # pointe vers un VRAI parent dans le système ou vers un message externe
    # (mail de Daniel envoyé depuis une autre boîte, jamais ingéré).
    known_message_ids: set[str] = set()
    for source in (mails, all_thread_siblings or []):
        for m in source:
            mid = (m.get("message_id") or "").strip()
            if mid:
                known_message_ids.add(mid)

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
                    "parent_is_orphan": False,
                    "all_orphans": False,
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
                "_all_known_message_ids": set(),  # v1.30.0.8 — pour détection intra-thread
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
            # v1.30.0.5 — BUG FIX : reply_count n'était jamais incrémenté !
            # Conséquence : un fil avec parent + reply voyait reply_count=0,
            # le template affichait `mail_row(parent)` au lieu de thread_row
            # → la reply était invisible (rendue uniquement si `t.replies`
            # itéré manuellement, ce qui n'était jamais le cas).
            t["reply_count"] = len(t["replies"])
            # last_received
            if (mail.get("received_at") or "") > t["last_received"]:
                t["last_received"] = mail.get("received_at") or t["last_received"]
        # v1.30.0.8 — accumule les message_id intra-thread
        mid = (mail.get("message_id") or "").strip()
        if mid:
            threads_dict[tid]["_all_known_message_ids"].add(mid)
        if mail.get("status") != "duplicate":
            threads_dict[tid]["all_duplicate"] = False

    # v1.30.0.8 — pour chaque fil, recalcule le parent en excluant les replies
    # orphelins. Le parent = le mail le plus ancien du fil qui n'est PAS un
    # orphan reply (a un in_reply_to qui pointe vers un mail absent du système).
    # Cas 1 : au moins un mail non-orphelin dans le fil → parent = le plus
    # ancien non-orphelin, les autres (orphelins ou non) sont en replies.
    # Cas 2 : TOUS les mails du fil sont orphelins → parent = le plus ancien
    # (c'est le "premier mail connu" du fil), `parent_is_orphan=True`,
    # `all_orphans=True` → rendu sans icône `›` sur le parent.
    for tid, t in threads_dict.items():
        all_mails_in_thread = [t["parent"]] + t["replies"]
        same_thread_msgids = t.get("_all_known_message_ids", set())
        # Identifie les orphans et les non-orphans
        non_orphans = [
            m for m in all_mails_in_thread
            if not _is_orphan_reply(m, known_message_ids, same_thread_msgids)
        ]
        if non_orphans:
            # Le parent = le plus ancien non-orphan
            new_parent = min(
                non_orphans,
                key=lambda m: m.get("received_at") or "",
            )
            # Reconstruit la liste des replies (tout le reste)
            new_replies = [m for m in all_mails_in_thread if m["id"] != new_parent["id"]]
            # Tri replies du + récent au + ancien
            new_replies.sort(key=lambda m: m.get("received_at") or "", reverse=True)
            t["parent"] = new_parent
            t["replies"] = new_replies
            t["reply_count"] = len(new_replies)
            t["parent_is_orphan"] = False
            t["all_orphans"] = False
        else:
            # TOUS les mails du fil sont des replies orphelins
            # → le parent = le plus ancien (premier mail connu), pas d'icône ›
            # v1.30.0.12 — on trie les replies du + récent au + ancien (sinon
            # elles apparaissent dans l'ordre d'insertion initial, qui n'est
            # pas forcément chronologique).
            sorted_replies = sorted(
                [m for m in all_mails_in_thread if m["id"] != t["parent"]["id"]],
                key=lambda m: m.get("received_at") or "",
                reverse=True,
            )
            t["replies"] = sorted_replies
            t["reply_count"] = len(sorted_replies)
            t["parent_is_orphan"] = True
            t["all_orphans"] = True
        # Nettoie la clé interne
        t.pop("_all_known_message_ids", None)

    # v1.29.1.5 — si le parent d'un fil a un status non-pending (approved/rejected/sent/reviewed),
    # c'est qu'il a déjà été traité par Daniel. Le grouper avec une reply pending n'apporte
    # aucune valeur visuelle (Daniel ne peut plus rien faire sur le parent). On ÉCLATE le fil
    # en orphelins pour que chaque mail pending s'affiche comme un mail simple (sans `›`,
    # sans bordure purple) — c'est cohérent avec la sémantique "1 mail pending = 1 ligne".
    # Cas concret : le bucket `adhoc::unknown::50d8b4a9` regroupe 207 mails sans rapport
    # (Infomaniak, Coolblue, formations, etc.) sous un parent "Un message du fondateur
    # d'Infomaniak" déjà approved. Éclater le fil règle le bug "reply sans parent visible".
    #
    # v1.30.0.5 — split en 2 listes : les replies pending dont le parent est non-pending
    # doivent DÉMÉNAGER dans l'autre band (pas dans la hot si la hot est la band d'origine).
    # Un reply dont le parent est déjà traité n'a pas de sens en hot band : Daniel ne peut
    # PLUS rien faire dessus (le parent est clos). Il doit vivre dans OTHER pour archivage.
    # v1.30.0.12 — on ne déplace plus les 1-mail "Re:" orphelins (sujet commence par Re:
    # mais pas de thread_id) : ils restent visibles dans la liste d'origine (Daniel
    # veut voir TOUS ses mails, cf. v1.30.0.11). Le move-to-other n'est appliqué que
    # pour les VRAIS cas de cross-band (parent dans other → reply pending dans hot).
    final_keep: list[dict] = []
    final_move: list[dict] = []
    for t in list(threads_dict.values()) + orphans:
        parent_status = (t["parent"].get("status") or "").lower()
        if parent_status and parent_status != "pending":
            # Parent déjà traité → on convertit toutes les replies en orphelins
            # et on les DÉPLACE dans l'autre band (le parent est clos, ces
            # replies pending n'ont plus rien à faire avec le scope d'origine).
            for r in t["replies"]:
                final_move.append(
                    {
                        "thread_id": f"orphan::{r['id']}",
                        "parent": r,
                        "replies": [],
                        "reply_count": 0,
                        "last_received": r.get("received_at") or r.get("processed_at") or "",
                        "all_duplicate": r.get("status") == "duplicate",
                        "parent_is_orphan": False,
                        "all_orphans": False,
                    }
                )
            # v1.30.0.11 — le parent lui-même est aussi déplacé. Avant, le
            # commentaire disait "il n'est pas dans la liste d'origine" — c'est
            # vrai pour les parents qui n'ont pas le statut pending (déjà filtrés
            # par _fetch_mails), mais FAUX pour les parents qui SONT dans la
            # liste d'origine avec un statut non-pending (ex: status='duplicate'
            # qui passe par le WHERE `pending OR NULL` puis est exclu du hot et
            # tombe dans other). Sans ce fix, on perdait le mail 306 (doublon
            # dans le test worklist). On l'ajoute en `final_move` pour qu'il
            # apparaisse dans l'autre band (other).
            final_move.append(t)
        else:
            # Parent pending (ou thread à 1 seul mail pending) : on le garde
            # MAIS on vérifie aussi qu'il ne s'agit pas d'un reply orphelin
            # CROSS-BAND (parent dans l'autre liste = déjà traité).
            # Cas 1 : thread_id présent + siblings dans `all_thread_siblings`
            # (other_mails) → le parent est dans other (déjà traité), ce mail
            # est un reply → MOVE.
            # v1.30.0.12 — on NE déplace plus les 1-mail "Re:" orphelins
            # (sujet Re: sans thread_id, sans siblings). Daniel veut les voir
            # dans la liste d'origine, pas dans other. C'est le rollback
            # v1.30.0.11 confirmé.
            # v1.30.0.13 — on vérifie EN PLUS que le parent est un reply (sujet
            # préfixé Re:/AW:/Fwd: OU in_reply_to orphelin). Sans ça, on
            # déplace aussi le VRAI parent quand sa reply a été exclue du hot
            # par le filtre Re: SQL — le parent n'a aucune raison de déménager
            # dans other. Cas test `test_hot_band_keeps_pending_parent_with_pending_reply` :
            # parent id=200 (sujet non-Re) + reply id=201 (Re:…, exclue du hot
            # mais dans other via SQL). Le parent doit RESTER en hot band.
            siblings_for_thread = siblings_by_tid.get(t["parent"].get("thread_id"), [])
            same_thread_msgids = {s.get("message_id") for s in siblings_for_thread if s.get("message_id")}
            is_reply_in_other = (
                t["reply_count"] == 0
                and t["parent"].get("thread_id")
                and t["parent"].get("status", "pending") in (None, "", "pending")
                and siblings_for_thread
                and _is_orphan_reply(t["parent"], known_message_ids, same_thread_msgids)
            )
            if is_reply_in_other:
                # Reply orphelin cross-band (parent ailleurs, déjà traité) → move
                final_move.append(t)
            else:
                final_keep.append(t)

    # Tri global par date du mail le plus récent DESC (chaque liste séparément)
    final_keep.sort(key=lambda t: t["last_received"], reverse=True)
    final_move.sort(key=lambda t: t["last_received"], reverse=True)
    return final_keep, final_move


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
    # v1.30.0.6 — le paramètre `view` est IGNORÉ. Seule la vue Fils existe désormais.
    # v1.30.0.11 — rollback du worklist mode : "Toutes" affiche TOUS les mails
    # répartis en 2 bandes (hot verte + other grise). Plus de masquage.

    hot_mails, other_mails = await _fetch_mails(
        db, boxes, category, status, priority, q, sort_col, sort_order,
    )

    # v1.30.0.6 — vue unique = threads. CDAL ne veut QUE des fils de discussion.
    # Les anciennes vues `flat` (1 ligne = 1 mail) et `duplicates` (audit v1.28.3)
    # sont supprimées : si l'URL contient ?view=flat ou ?view=duplicates, on
    # force `view='threads'` (le param est ignoré silencieusement).
    view = "threads"
    # v1.30.0.5 — on groupe les hot_mails en fils, en passant other_mails comme
    # siblings pour détecter les cross-band moves (reply pending dont le parent
    # est déjà traité dans other).
    # v1.30.0.11 — on ne groupe PAS other_mails (rollback de la double-grouping).
    # other_mails a déjà la bonne catégorisation (hot exclude clauses), on
    # les passe tels quels pour éviter que des "Re:" 1-mail threads soient
    # déplacés dans hot_move par le grouping. Tout doit rester visible.
    # v1.30.0.12 — on RE-GROUPE other_mails en fils. Justification : 3 mails
    # "Re: Votre reçu Apple" (même thread_id) étaient affichés comme 3 lignes
    # plates individuelles (1-mail thread par mail) → CDAL : "un enfant d'un
    # fil parent ne peut pas démarrer seul". En groupant, ils deviennent 1
    # fil avec le plus ancien comme parent (sans › car tous Re: → parent
    # orphelin) et les 2 autres enfilés en replies avec ›.
    hot_keep, hot_move = _group_into_threads(hot_mails, all_thread_siblings=other_mails)
    hot_threads = hot_keep
    # Regroupe other_mails aussi (les "Re: ..." d'un même thread deviennent 1 fil)
    other_keep, other_move = _group_into_threads(other_mails)
    other_threads = other_keep + hot_move + other_move

    # v1.30.0.11 — on n'écrase plus other_threads. Tout est visible : la hot
    # band verte (demande_client+urgent pending) en haut, puis la other band
    # grise (tout le reste : replies, traités, doublons, autres catégories).

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
