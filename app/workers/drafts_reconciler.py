"""Réconcilieur Drafts IMAP — garantit que toute proposition générée est physiquement
présente dans le dossier Drafts de la boîte source.

v1.25.22 — garde-fou anti-crash silencieux demandé par CDAL (mail #629 : delivered_at
set en DB mais brouillon absent de Drafts, Daniel ne retrouvait pas sa proposition).

Toutes les 15 min :
  1. liste les mail `demande_client` avec un `ai_draft` non vide (récents).
  2. pour chaque boîte concernée, ouvre 1 connexion IMAP et SELECT Drafts.
  3. pour chaque mail, cherche le brouillon par header `X-Detective-Mail-Id` (v1.25.22+)
     puis par body `EMAIL #<id>` (legacy livrés avant le marker).
  4. si présent et `delivered_at` NULL -> set `delivered_at` (sync DB).
  5. si absent -> re-livrer via `append_draft` + alerter Slack. Si re-livraison KO ->
     alerte Slack (intervention requise).

On privilégie les faux positifs (re-livrer à tort -> doublon, gérable) aux faux négatifs
(brouillon manquant -> Daniel perd une proposition, intolérable).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import aiosqlite
import structlog
from aioimaplib import aioimaplib

from app.config import MailboxConfig, get_settings
from app.delivery.imap_draft import _find_drafts_folder, append_draft
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult
from app.pipeline.language import detect_language

log = structlog.get_logger()

RECONCILE_INTERVAL_MINUTES = 15
# On ne scanne que les mail traités récemment (perf + pertinence).
SCOPE_DAYS = 30
# Anti-bruit : un même mail_id n'est alerté "manquant" qu'une fois par cycle de rouge.
_missing_alerted: set[int] = set()


def _scope_cutoff_iso() -> str:
    now = datetime.now(UTC)
    cutoff = now.timestamp() - SCOPE_DAYS * 86400
    return datetime.fromtimestamp(cutoff, tz=UTC).isoformat()


async def _fetch_candidates(db_path) -> list[dict]:
    """Mail demande_client avec brouillon, traités dans les SCOPE_DAYS derniers jours."""
    cutoff = _scope_cutoff_iso()
    sql = """
        SELECT id, imap_uid, mailbox_name, subject, sender,
               received_at, ai_draft, body, delivered_at, reply_to
        FROM mail_processed
        WHERE lower(category) = 'demande_client'
          AND draft_generated = 1
          AND ai_draft IS NOT NULL
          AND ai_draft != ''
          AND processed_at IS NOT NULL
          AND processed_at >= ?
        ORDER BY id DESC
        LIMIT 200
    """
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (cutoff,)) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def _draft_present(client: aioimaplib.IMAP4, folder: str, mail_id: int) -> bool:
    """True si un brouillon correspondant à mail_id est présent dans `folder`.

    Recherche par header `X-Detective-Mail-Id` (v1.25.22+), fallback par body
    `EMAIL #<id>` pour les brouillons legacy livrés avant le marker.
    """
    try:
        sel = await client.select(folder)
        if sel.result != "OK":
            return False
    except Exception:
        return False
    # 1) marker header (précis, nouveaux brouillons)
    try:
        resp = await client.search(f"HEADER X-Detective-Mail-Id {mail_id}")
        if resp.result == "OK":
            for line in resp.lines or []:
                if line.strip():
                    return True
    except Exception:
        pass
    # 2) fallback body marker (legacy) — "EMAIL #629" présent dans le corps du brouillon
    try:
        resp = await client.search(f'BODY "EMAIL #{mail_id}"')
        if resp.result == "OK":
            for line in resp.lines or []:
                if line.strip():
                    return True
    except Exception:
        pass
    return False


async def _set_delivered(db_path, mail_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE mail_processed SET delivered_at = ? WHERE id = ?",
            (now, mail_id),
        )
        await db.commit()


def _rebuild_inputs(mail: dict, mailbox: MailboxConfig) -> tuple[IncomingMail, GenerationResult]:
    """Reconstruit IncomingMail + GenerationResult depuis la ligne DB (pour append_draft)."""
    language = detect_language(mail.get("body") or "", default=mailbox.default_lang)
    incoming = IncomingMail(
        sender=mail.get("sender") or "",
        subject=mail.get("subject") or "",
        body=mail.get("body") or "",
        received_at=mail.get("received_at") or "",
        message_id=mail.get("imap_uid") or "",
        reply_to=mail.get("reply_to") or "",
    )
    gen = GenerationResult(
        draft=mail["ai_draft"],
        raw_draft=mail["ai_draft"],
        language=language,
        rag_pairs=[],
        model_used="",
        category="demande_client",
        vault_notes=[],
    )
    return incoming, gen


async def _alert_missing(mail_id: int, mailbox_name: str, subject: str, relivered: bool) -> None:
    """Alerte Slack best-effort quand un brouillon est manquant."""
    try:
        from app.delivery.slack_notifier import send_slack_message

        status = "re-livré" if relivered else "RE-LIVRAISON ÉCHOUÉE — intervention requise"
        text = (
            f"⚠️ Réconcilieur Drafts — brouillon #{mail_id} manquant dans Drafts de "
            f"`{mailbox_name}` (sujet DB : {(subject or '')[:60]}). {status}."
        )
        await send_slack_message(text)
    except Exception as exc:
        log.warning("reconcile.alert_slack_failed", mail_id=mail_id, error=str(exc))


async def _reconcile_mailbox(
    mailbox: MailboxConfig,
    mails: list[dict],
    settings,
) -> tuple[int, int, int]:
    """Traite tous les mail d'une boîte sur une seule connexion IMAP.

    Retourne (present, redelivered, still_missing).
    """
    if not mails:
        return (0, 0, 0)
    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        await client.wait_hello_from_server()
        login_resp = await client.login(mailbox.user, mailbox.app_password)
        if login_resp.result != "OK":
            log.warning("reconcile.login_failed", mailbox=mailbox.name)
            return (0, 0, len(mails))
        folder = await _find_drafts_folder(client)
        if not folder:
            log.warning("reconcile.no_drafts_folder", mailbox=mailbox.name)
            return (0, 0, len(mails))

        present = redelivered = missing = 0
        for mail in mails:
            mail_id = mail["id"]
            try:
                found = await _draft_present(client, folder, mail_id)
            except Exception as exc:
                log.warning("reconcile.check_error", mail_id=mail_id, error=str(exc))
                found = False

            if found:
                present += 1
                if not mail.get("delivered_at"):
                    await _set_delivered(settings.db_agent_state, mail_id)
                    log.info("reconcile.sync_delivered", mail_id=mail_id, mailbox=mailbox.name)
                _missing_alerted.discard(mail_id)
                continue

            # Absent -> re-livrer
            incoming, gen = _rebuild_inputs(mail, mailbox)
            try:
                ok = await append_draft(incoming, mailbox, gen, mail_id=mail_id, imap_client=client)
            except Exception as exc:
                log.error("reconcile.redeliver_exception", mail_id=mail_id, error=str(exc))
                ok = False

            if ok:
                redelivered += 1
                await _set_delivered(settings.db_agent_state, mail_id)
                log.warning(
                    "reconcile.redelivered",
                    mail_id=mail_id,
                    mailbox=mailbox.name,
                    subject=(mail.get("subject") or "")[:60],
                )
                await _alert_missing(
                    mail_id, mailbox.name, mail.get("subject") or "", relivered=True
                )
            else:
                missing += 1
                if mail_id not in _missing_alerted:
                    _missing_alerted.add(mail_id)
                    log.error(
                        "reconcile.still_missing",
                        mail_id=mail_id,
                        mailbox=mailbox.name,
                        subject=(mail.get("subject") or "")[:60],
                    )
                    await _alert_missing(
                        mail_id, mailbox.name, mail.get("subject") or "", relivered=False
                    )
        return (present, redelivered, missing)
    finally:
        with contextlib.suppress(Exception):
            await client.logout()


async def reconcile_once() -> dict:
    """Passe unique de réconciliation. Retourne un bilan par boîte + total."""
    settings = get_settings()
    candidates = await _fetch_candidates(settings.db_agent_state)
    if not candidates:
        log.info("reconcile.no_candidates")
        return {"total": 0}

    # Grouper par boîte
    by_mailbox: dict[str, list[dict]] = {}
    for m in candidates:
        by_mailbox.setdefault(m["mailbox_name"], []).append(m)

    totals = {"present": 0, "redelivered": 0, "missing": 0}
    per_mailbox: dict[str, dict] = {}
    for mb in settings.mailboxes():
        mails = by_mailbox.get(mb.name, [])
        if not mails:
            continue
        present, redelivered, missing = await _reconcile_mailbox(mb, mails, settings)
        per_mailbox[mb.name] = {"present": present, "redelivered": redelivered, "missing": missing}
        totals["present"] += present
        totals["redelivered"] += redelivered
        totals["missing"] += missing

    log.info(
        "reconcile.cycle_done",
        total=len(candidates),
        **totals,
        per_mailbox=per_mailbox,
    )
    return {"total": len(candidates), **totals, "per_mailbox": per_mailbox}


async def watch_drafts(stop_event: asyncio.Event) -> None:
    """Tâche de fond : réconciliation Drafts IMAP toutes les 15 min."""
    interval = RECONCILE_INTERVAL_MINUTES * 60
    # 1re passe 30s après le boot (ne pas attendre 15 min pour la 1e vérif)
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30)
        return
    except TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            await reconcile_once()
        except Exception as exc:
            log.warning("reconcile.cycle_error", error=str(exc))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
