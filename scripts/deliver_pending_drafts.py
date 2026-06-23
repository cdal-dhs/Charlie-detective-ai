"""Délivre en IMAP Drafts les brouillons déjà générés en base mais pas encore
dans les Drafts de la boîte source.

Contexte : le backfill (`scripts/backfill_reclassify.py`) régénère des
brouillons pour des mails historiques. Le poller IMAP ne re-livre PAS ces
brouillons (condition `is_new` dans `imap_poller.py:1298`). Sans ce script,
Daniel ne verrait jamais les brouillons des mails reclassifiés.

Ce script :
  1. Lit mail_processed WHERE category='demande_client' AND draft_generated=1
     AND delivered_at IS NULL (ou flag équivalent).
  2. Pour chaque mail, appelle append_draft() pour déposer le brouillon
     dans les Drafts IMAP de la boîte source.
  3. Marque delivered_at (nouvelle colonne) pour ne pas redélivrer.

Usage :
  python -m scripts.deliver_pending_drafts                  # dry-run par défaut
  python -m scripts.deliver_pending_drafts --apply          # délivre vraiment
  python -m scripts.deliver_pending_drafts --limit 20       # limite à N
  python -m scripts.deliver_pending_drafts --only-id 504    # délivre un seul
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

# Permettre l'import depuis la racine du projet
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import append_draft
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult

log = structlog.get_logger()


async def _ensure_column(db_path: Path) -> None:
    """Ajoute la colonne delivered_at si elle n'existe pas (idempotent)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(mail_processed)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "delivered_at" not in cols:
            log.info("deliver.add_column", column="delivered_at")
            await db.execute("ALTER TABLE mail_processed ADD COLUMN delivered_at TEXT")
            await db.commit()


async def _fetch_pending(
    db_path: Path, only_id: int | None = None, limit: int | None = None
) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT id, imap_uid, mailbox_name, subject, sender, received_at,
                   ai_draft, body
            FROM mail_processed
            WHERE category = 'demande_client'
              AND draft_generated = 1
              AND ai_draft IS NOT NULL
              AND ai_draft != ''
              AND delivered_at IS NULL
        """
        params: list = []
        if only_id is not None:
            sql += " AND id = ?"
            params.append(only_id)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with db.execute(sql, params) as cur:
            return [dict(row) for row in await cur.fetchall()]


def _find_mailbox(name: str) -> object | None:
    for mb in get_settings().mailboxes():
        if mb.name == name:
            return mb
    return None


def _sanitize_subject(subject: str) -> str:
    """Nettoie les caractères interdits dans les headers MIME (\\n, \\r).

    v1.22.2 : les invitations Google Calendar et certaines convocations
    contiennent des \\r\\n dans le sujet — interdit par RFC 5322. On
    remplace par espace pour préserver la lisibilité.
    """
    return subject.replace("\r", " ").replace("\n", " ").strip()


async def _mark_delivered(db_path: Path, mail_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE mail_processed SET delivered_at = ? WHERE id = ?",
            (now, mail_id),
        )
        await db.commit()


async def main(apply: bool, limit: int | None, only_id: int | None) -> None:
    settings = get_settings()
    log.info(
        "deliver.start",
        apply=apply,
        limit=limit,
        only_id=only_id,
        db=str(settings.db_agent_state),
    )

    await _ensure_column(settings.db_agent_state)

    pending = await _fetch_pending(settings.db_agent_state, only_id=only_id, limit=limit)
    log.info("deliver.candidates", count=len(pending))

    if not apply:
        log.info(
            "deliver.dry_run_note",
            message="Aucun dépôt IMAP. Relancer avec --apply pour livrer.",
        )

    delivered = 0
    failed = 0
    skipped = 0

    for i, mail in enumerate(pending, 1):
        mailbox = _find_mailbox(mail["mailbox_name"])
        if mailbox is None:
            log.warning(
                "deliver.no_mailbox",
                mail_id=mail["id"],
                mailbox_name=mail["mailbox_name"],
            )
            skipped += 1
            continue

        incoming = IncomingMail(
            sender=mail["sender"] or "",
            subject=_sanitize_subject(mail["subject"] or ""),
            body=mail.get("body")
            or "",  # v1.25.9 — passe le body original pour qu'il apparaisse dans le brouillon IMAP
            received_at=mail["received_at"] or "",
            message_id=mail.get("imap_uid") or "",  # fallback sur imap_uid
        )
        gen = GenerationResult(
            draft=mail["ai_draft"],
            raw_draft=mail["ai_draft"],  # non utilisé par append_draft
            language="fr",  # non utilisé par append_draft
            rag_pairs=[],
            model_used="",  # non utilisé par append_draft
            category="demande_client",  # non utilisé par append_draft
            vault_notes=[],
        )

        if not apply:
            log.info(
                "deliver.dry_run_would_deliver",
                mail_id=mail["id"],
                mailbox=mailbox.name,
                sender=(mail["sender"] or "")[:40],
            )
            continue

        try:
            ok = await append_draft(incoming, mailbox, gen, mail_id=mail["id"])
        except Exception as exc:
            log.error("deliver.exception", mail_id=mail["id"], error=str(exc))
            ok = False

        if ok:
            await _mark_delivered(settings.db_agent_state, mail["id"])
            delivered += 1
            log.info(
                "deliver.ok",
                mail_id=mail["id"],
                mailbox=mailbox.name,
                sender=(mail["sender"] or "")[:40],
            )
        else:
            failed += 1
            log.warning(
                "deliver.failed",
                mail_id=mail["id"],
                mailbox=mailbox.name,
            )

        if i % 10 == 0:
            log.info("deliver.progress", processed=i, total=len(pending))

    log.info(
        "deliver.done",
        apply=apply,
        candidates=len(pending),
        delivered=delivered,
        failed=failed,
        skipped=skipped,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Délivre les brouillons existants en IMAP Drafts (v1.22.1)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Dépose réellement les brouillons (sinon dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite le nombre de mails traités (pour test)",
    )
    parser.add_argument(
        "--only-id",
        type=int,
        default=None,
        help="Délivre un seul mail par son ID",
    )
    args = parser.parse_args()

    asyncio.run(main(apply=args.apply, limit=args.limit, only_id=args.only_id))
