"""Backfill : reclassifie les mails historique (catégorie autre/facture/rappel/urgent)
avec le classifier v1.22.1 (durci anti-oubli demande_client).

Contexte : hotfix v1.22.1. Le classifier LLM classait certains mails client
en 'facture' (à cause d'un 'Re:' + citation d'un devis passé) au lieu de
'demande_client'. Conséquence : pas de brouillon généré pour ces clients.

Ce script :
  1. Lit les mails avec category IN (autre, facture, rappel, urgent) ET draft_generated=0
  2. Pour chacun : appelle classify() (LLM + post-traitement anti-oubli)
  3. Si nouvelle catégorie = demande_client :
     a. Update category = 'demande_client', status='pending', priority='high'
     b. Régénère un brouillon via generate_draft() + persiste en base
     c. Log 'backfill.demande_client_found'

Usage :
  python -m scripts.backfill_reclassify             # dry-run par défaut
  python -m scripts.backfill_reclassify --apply     # applique les changements
  python -m scripts.backfill_reclassify --limit 20  # limite à N mails
  python -m scripts.backfill_reclassify --only-id 504  # reclassifie un seul mail
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

# Permettre l'import depuis la racine du projet
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import MailboxConfig, get_settings
from app.pipeline.classifier import classify
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language

log = structlog.get_logger()


async def _fetch_candidates(
    db_path: Path,
    categories: tuple[str, ...] = ("autre", "facture", "rappel", "urgent"),
    only_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        # v1.24.1 — quand un ID précis est ciblé (--only-id), on ne filtre PLUS par
        # catégorie : CDAL sait quel mail il veut retraiter (ex: un phishing mal
        # classé qu'on veut remonter en demande_client après le hardening). On garde
        # uniquement le filtre « pas encore de brouillon généré ».
        if only_id is not None:
            sql = """
                SELECT id, imap_uid, mailbox_name, subject, sender, received_at, category,
                       status, priority, body, length(ai_draft) as draft_len
                FROM mail_processed
                WHERE (ai_draft IS NULL OR ai_draft = '')
                  AND draft_generated = 0
                  AND id = ?
            """
            params: list = [only_id]
        else:
            where_cats = ",".join("?" * len(categories))
            sql = f"""
                SELECT id, imap_uid, mailbox_name, subject, sender, received_at, category,
                       status, priority, body, length(ai_draft) as draft_len
                FROM mail_processed
                WHERE category IN ({where_cats})
                  AND (ai_draft IS NULL OR ai_draft = '')
                  AND draft_generated = 0
            """
            params = list(categories)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _find_mailbox(name: str, mailboxes: list[MailboxConfig]) -> MailboxConfig | None:
    for mb in mailboxes:
        if mb.name == name:
            return mb
    return None


async def _regenerate_draft(
    mail: dict, mailbox: MailboxConfig, apply: bool
) -> tuple[str, str, str]:
    """Reclassifie + génère brouillon. Retourne (old_cat, new_cat, draft)."""
    body = mail["body"] or ""
    subject = mail["subject"] or ""
    sender = mail["sender"] or ""

    new_cat = await classify(subject, body, sender)

    draft = ""
    if new_cat == "demande_client" and apply:
        language = detect_language(body, default=mailbox.default_lang)
        result = await generate_draft(
            incoming_subject=subject,
            incoming_body=body,
            sender=sender,
            mailbox=mailbox,
            language=language,
            category=new_cat,
        )
        draft = result.draft

    return mail["category"], new_cat, draft


def _update_db(
    db_path: Path,
    mail_id: int,
    new_category: str,
    draft: str,
    apply: bool,
) -> None:
    """Update category + ai_draft + status + priority en base."""
    if not apply:
        return
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        if new_category == "demande_client":
            conn.execute(
                """
                UPDATE mail_processed SET
                    category = ?,
                    status = 'pending',
                    priority = 'high',
                    ai_draft = ?,
                    draft_generated = 1
                WHERE id = ?
                """,
                (new_category, draft, mail_id),
            )
        else:
            conn.execute(
                "UPDATE mail_processed SET category = ? WHERE id = ?",
                (new_category, mail_id),
            )
        conn.commit()
    finally:
        conn.close()


async def main(apply: bool, limit: int | None, only_id: int | None) -> None:
    settings = get_settings()
    log.info(
        "backfill.start",
        apply=apply,
        limit=limit,
        only_id=only_id,
        db=str(settings.db_agent_state),
    )

    candidates = await _fetch_candidates(
        settings.db_agent_state, only_id=only_id, limit=limit
    )
    log.info("backfill.candidates", count=len(candidates))

    demade_count = 0
    reclassed_count = 0
    skipped_count = 0

    for i, mail in enumerate(candidates, 1):
        mailbox = _find_mailbox(mail["mailbox_name"], settings.mailboxes())
        if mailbox is None:
            log.warning(
                "backfill.no_mailbox",
                mail_id=mail["id"],
                mailbox_name=mail["mailbox_name"],
            )
            skipped_count += 1
            continue

        try:
            old_cat, new_cat, draft = await _regenerate_draft(mail, mailbox, apply)
        except Exception as e:
            log.error(
                "backfill.error",
                mail_id=mail["id"],
                error=str(e),
                subject=(mail["subject"] or "")[:60],
            )
            skipped_count += 1
            continue

        reclassed_count += 1
        if new_cat == "demande_client":
            demade_count += 1
            log.info(
                "backfill.demande_client_found",
                mail_id=mail["id"],
                old_category=old_cat,
                new_category=new_cat,
                mailbox=mail["mailbox_name"],
                sender=(mail["sender"] or "")[:50],
                subject=(mail["subject"] or "")[:60],
                apply=apply,
                draft_length=len(draft) if draft else 0,
            )

        # Update DB
        try:
            _update_db(
                settings.db_agent_state, mail["id"], new_cat, draft, apply
            )
        except Exception as e:
            log.error("backfill.db_update_failed", mail_id=mail["id"], error=str(e))
            skipped_count += 1
            reclassed_count -= 1

        if i % 10 == 0:
            log.info("backfill.progress", processed=i, total=len(candidates))

    log.info(
        "backfill.done",
        apply=apply,
        candidates=len(candidates),
        reclassed=reclassed_count,
        demade_client_found=demade_count,
        skipped=skipped_count,
    )

    if not apply:
        log.info("backfill.dry_run_note", message="Aucun changement appliqué. Relancer avec --apply pour appliquer.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill : reclassifie les mails mal catégorisés (v1.22.1)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique réellement les changements en base (sinon dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite le nombre de mails reclassifiés (pour test)",
    )
    parser.add_argument(
        "--only-id",
        type=int,
        default=None,
        help="Reclassifie un seul mail par son ID",
    )
    args = parser.parse_args()

    asyncio.run(main(apply=args.apply, limit=args.limit, only_id=args.only_id))
