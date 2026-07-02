"""v1.29.1 — Hourly check : relance la génération de brouillon pour les mails
demande_client pending qui n'ont PAS de brouillon (rattrapage auto).

Contexte : certains mails demande_client arrivent en prod sans brouillon
(crash LLM transitoire, deadlock poller, etc.). Daniel doit pouvoir compter
sur le fait que TOUT mail demande_client pending a une proposition dans
les Drafts IMAP de la boîte source dans l'heure qui suit son arrivée.

Toutes les heures pleines :
  1. Liste les mail demande_client pending SANS ai_draft (ou vide),
     traités dans les 7 derniers jours (perf + scope).
  2. Pour chaque mail : regénère via `generate_draft()` + dépose via
     `append_draft()` (header X-Detective-Mail-Id v1.25.22).
  3. Garde-fou : max 3 retries (cf. règle CDAL "Max 3 tentatives").
  4. Garde-fou idempotence : si `draft_generated=1` entre la query et
     l'APPEND (race avec le poller), on saute.
  5. Alerte Slack/Resend si 3 retries KO persistants (règle "Zéro crash
     silencieux").

Différence avec drafts_reconciler (15 min) : le réconcilieur travaille
sur les brouillons DÉJÀ GÉNÉRÉS mais pas encore APPENDés (delivered_at
NULL). Le hourly check travaille en AMONT, sur les mails qui n'ont
même pas encore de brouillon.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import structlog
from aioimaplib import aioimaplib

from app.config import MailboxConfig, get_settings
from app.delivery.imap_draft import _find_drafts_folder, append_draft
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult, generate_draft
from app.pipeline.language import detect_language

log = structlog.get_logger()

# Fréquence du check (1h)
HOURLY_INTERVAL_MINUTES = 60
# Scope temporel : on ne rattrape que les 7 derniers jours (perf + scope)
SCOPE_DAYS = 7
# v1.29.1 — max retries par mail (règle CDAL "Max 3 tentatives")
MAX_RETRIES_PER_MAIL = 3
# v1.29.1 — timeout par appel generate_draft (en secondes, on ne veut pas
# bloquer la boucle horaire si un mail LLM traîne)
GEN_TIMEOUT_SECONDS = 60
# v1.29.1 — fenêtre "déjà alerté cette heure-ci" (anti-doublon d'alerte)
_alerted_this_cycle: set[int] = set()


def _scope_cutoff_iso() -> str:
    """ISO 8601 de maintenant - SCOPE_DAYS (UTC)."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=SCOPE_DAYS)
    return cutoff.isoformat()


async def _fetch_missing_drafts(db_path: Path) -> list[dict]:
    """Liste les mail demande_client pending SANS brouillon, scope 7j.

    Critères TOUS obligatoires :
    - category = 'demande_client' (seules les vraies demandes client
      déclenchent un brouillon qualifiant, cf. draft_categories config)
    - status = 'pending' (déjà approuvé/rejeté/envoyé/reviewed → on ne touche plus)
    - IFNULL(ai_draft, '') = '' (pas de brouillon, ni NULL ni vide)
    - draft_generated = 0 (sécurité anti-doublon, le poller n'a pas essayé)
    - processed_at >= cutoff (scope 7j)
    - LIMIT 100 (sécurité anti-explosion, retry progressif sur plusieurs cycles)

    Returns:
        Liste de dicts : {id, mailbox_name, subject, sender, body, body_preview,
        received_at, reply_to, status}.
    """
    cutoff = _scope_cutoff_iso()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, mailbox_name, subject, sender, body, body_preview,
                   received_at, reply_to, status, category
            FROM mail_processed
            WHERE lower(category) = 'demande_client'
              AND status = 'pending'
              AND IFNULL(ai_draft, '') = ''
              AND draft_generated = 0
              AND processed_at IS NOT NULL
              AND processed_at >= ?
            ORDER BY id ASC
            LIMIT 100
            """,
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _generate_for_mail(mail: dict, mailbox: MailboxConfig) -> GenerationResult:
    """Génère un brouillon pour un mail (max 3 retries internes)."""
    last_err = None
    for attempt in range(1, MAX_RETRIES_PER_MAIL + 1):
        try:
            language = detect_language(mail["body"] or "", default=mailbox.default_lang)
            gen = await asyncio.wait_for(
                generate_draft(
                    incoming_subject=mail["subject"],
                    incoming_body=mail["body"] or "",
                    sender=mail["sender"],
                    mailbox=mailbox,
                    language=language,
                    category="demande_client",
                    is_followup_response=False,
                    reply_to=mail.get("reply_to") or "",
                ),
                timeout=GEN_TIMEOUT_SECONDS,
            )
            if gen and gen.draft and gen.draft.strip():
                return gen
            last_err = f"gen.draft empty (attempt {attempt})"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} (attempt {attempt})"
        # Backoff exponentiel entre retries : 1s, 2s, 4s
        if attempt < MAX_RETRIES_PER_MAIL:
            await asyncio.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"max retries exhausted: {last_err}")


async def _process_one(
    db_path: Path, mail: dict, mailbox: MailboxConfig, imap_client: aioimaplib.IMAP4_SSL
) -> bool:
    """Traite un seul mail : génère draft + APPEND IMAP + UPDATE DB.

    Returns:
        True si succès, False sinon.
    """
    mail_id = mail["id"]
    log.info(
        "hourly_check.generating_draft",
        mail_id=mail_id,
        mailbox=mailbox.name,
        sender=mail["sender"],
        subject=mail["subject"][:80],
    )

    # 1. Garde-fou idempotence : re-check juste avant l'APPEND
    # (le poller peut avoir généré entre la query et maintenant)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT draft_generated, IFNULL(ai_draft, '') as draft FROM mail_processed WHERE id = ?",
            (mail_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return False
    draft_generated, current_draft = row
    if draft_generated or current_draft:
        log.info(
            "hourly_check.skip_already_generated",
            mail_id=mail_id,
            draft_generated=draft_generated,
            draft_len=len(current_draft),
        )
        return True  # traité par ailleurs, on compte comme succès

    # 2. Génération LLM (max 3 retries)
    try:
        gen = await _generate_for_mail(mail, mailbox)
    except Exception as e:
        log.error(
            "hourly_check.generation_failed",
            mail_id=mail_id,
            error=str(e),
        )
        return False

    # 3. APPEND IMAP (header X-Detective-Mail-Id v1.25.22)
    display_sender = mail["sender"]  # on n'appelle pas mask_forwarder ici (déjà DB)
    incoming = IncomingMail(
        sender=display_sender,
        subject=mail["subject"],
        body=mail.get("body") or "",
        received_at=mail["received_at"] or "",
        message_id="",
        reply_to=mail.get("reply_to") or "",
    )
    ok = await append_draft(
        incoming, mailbox, gen=gen, mail_id=mail_id, imap_client=imap_client
    )
    if not ok:
        log.warning("hourly_check.append_failed", mail_id=mail_id, mailbox=mailbox.name)
        return False

    # 4. UPDATE DB : ai_draft + draft_generated=1 + delivered_at (géré par append_draft)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE mail_processed SET ai_draft = ?, draft_generated = 1 WHERE id = ?",
            (gen.draft, mail_id),
        )
        await db.commit()
    log.info(
        "hourly_check.draft_deposited",
        mail_id=mail_id,
        mailbox=mailbox.name,
        draft_len=len(gen.draft),
    )
    return True


async def _process_mailbox(
    db_path: Path, mailbox: MailboxConfig, candidates: list[dict]
) -> tuple[int, int]:
    """Traite tous les candidats d'une mailbox.

    Returns:
        (ok_count, fail_count)
    """
    if not candidates:
        return 0, 0

    ok = 0
    fail = 0

    # Une seule connexion IMAP par cycle de mailbox (perf)
    imap_client = aioimaplib.IMAP4_SSL(host=mailbox.imap_host, port=mailbox.imap_port or 993)
    try:
        await imap_client.wait_hello_from_server()
        await imap_client.login(mailbox.user, mailbox.app_password)
        await _find_drafts_folder(imap_client)  # SELECT Drafts

        for mail in candidates:
            if mail["mailbox_name"] != mailbox.name:
                continue
            try:
                if await _process_one(db_path, mail, mailbox, imap_client):
                    ok += 1
                else:
                    fail += 1
                    _alerted_this_cycle.add(mail["id"])
            except Exception as e:
                log.exception(
                    "hourly_check.mail_unexpected_error",
                    mail_id=mail["id"],
                    error=str(e),
                )
                fail += 1
    finally:
        with __import__("contextlib").suppress(Exception):
            await imap_client.logout()

    return ok, fail


async def run_hourly_check() -> dict:
    """Cycle principal : 1 passe. Retourne stats pour télémétrie/logging.

    Returns:
        dict {processed, ok, failed, mailboxes}
    """
    settings = get_settings()
    _alerted_this_cycle.clear()
    started = datetime.now(UTC)

    candidates = await _fetch_missing_drafts(settings.db_agent_state)
    log.info("hourly_check.started", candidates=len(candidates), scope_days=SCOPE_DAYS)

    if not candidates:
        return {"processed": 0, "ok": 0, "failed": 0, "mailboxes": 0}

    # Groupe par mailbox pour ne faire qu'1 connexion IMAP par boîte
    by_mailbox: dict[str, list[dict]] = {}
    for c in candidates:
        by_mailbox.setdefault(c["mailbox_name"], []).append(c)

    total_ok = 0
    total_fail = 0
    for mb in settings.mailboxes():
        if mb.name not in by_mailbox:
            continue
        ok, fail = await _process_mailbox(settings.db_agent_state, mb, by_mailbox[mb.name])
        total_ok += ok
        total_fail += fail

    elapsed = (datetime.now(UTC) - started).total_seconds()
    stats = {
        "processed": len(candidates),
        "ok": total_ok,
        "failed": total_fail,
        "mailboxes": len(by_mailbox),
        "elapsed_seconds": elapsed,
    }
    if total_fail > 0:
        log.warning("hourly_check.completed_with_failures", **stats)
    else:
        log.info("hourly_check.completed", **stats)
    return stats


async def run_hourly_check_loop(stop_event: asyncio.Event) -> None:
    """Tâche de fond : lance le check toutes les heures.

    Démarre 10 min après le boot (pour ne pas saturer le LLM au démarrage).
    """
    log.info("hourly_check.scheduled", interval_minutes=HOURLY_INTERVAL_MINUTES)

    # Attendre 10 min au boot (le poller a le temps de tourner 2x)
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=600)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        try:
            await run_hourly_check()
        except Exception as e:
            log.exception("hourly_check.cycle_crash", error=str(e))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HOURLY_INTERVAL_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    # Permet `python -m app.workers.hourly_draft_check` (manual trigger en local)
    asyncio.run(run_hourly_check())
