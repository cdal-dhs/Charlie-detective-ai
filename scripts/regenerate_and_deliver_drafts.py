"""Régénère et relivre en IMAP Drafts les brouillons legacy au format actuel.

Usage:
  python -m scripts.regenerate_and_deliver_drafts \
      --apply --ids 608,615,606,614,621,622,202,204,207,208,226,139,149,151
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import append_draft
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.classifier import _is_human_followup, _is_reply_to_daniel
from app.pipeline.generator import GenerationResult, generate_draft
from app.pipeline.language import detect_language

log = structlog.get_logger()


def _find_mailbox(name: str):
    for mb in get_settings().mailboxes():
        if mb.name == name:
            return mb
    return None


async def _regenerate_and_deliver(mail_id: int, apply: bool) -> bool:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_agent_state)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, imap_uid, mailbox_name, subject, sender, received_at, category, body
            FROM mail_processed
            WHERE id = ?
            """,
            (mail_id,),
        ).fetchone()
        if not row:
            log.warning("regen.not_found", mail_id=mail_id)
            return False

        mail = dict(row)
        mailbox = _find_mailbox(mail["mailbox_name"])
        if mailbox is None:
            log.warning("regen.no_mailbox", mail_id=mail_id, mailbox_name=mail["mailbox_name"])
            return False

        subject = mail["subject"] or ""
        body = mail["body"] or ""
        sender = mail["sender"] or ""

        language = detect_language(body, default=mailbox.default_lang)
        is_followup = _is_reply_to_daniel(body, sender) or _is_human_followup(subject, body, sender)

        log.info(
            "regen.generating",
            mail_id=mail_id,
            mailbox=mailbox.name,
            language=language,
            is_followup=is_followup,
        )

        if not apply:
            log.info("regen.dry_run", mail_id=mail_id)
            return True

        result = await generate_draft(
            incoming_subject=subject,
            incoming_body=body,
            sender=sender,
            mailbox=mailbox,
            language=language,
            category="demande_client",
            is_followup_response=is_followup,
        )

        # Persister le nouveau brouillon et forcer la relivraison
        conn.execute(
            """
            UPDATE mail_processed
            SET ai_draft = ?, draft_generated = 1, delivered_at = NULL
            WHERE id = ?
            """,
            (result.draft, mail_id),
        )
        conn.commit()

        incoming = IncomingMail(
            sender=sender,
            subject=subject,
            body=body,
            received_at=mail["received_at"] or "",
            message_id=mail.get("imap_uid") or "",
        )
        gen = GenerationResult(
            draft=result.draft,
            raw_draft=result.raw_draft,
            language=language,
            rag_pairs=result.rag_pairs,
            model_used=result.model_used,
            category="demande_client",
            vault_notes=result.vault_notes,
            suggested_subject=result.suggested_subject,
        )

        ok = await append_draft(incoming, mailbox, gen, mail_id=mail_id)
        if ok:
            now = datetime.now(UTC).isoformat()
            conn.execute("UPDATE mail_processed SET delivered_at = ? WHERE id = ?", (now, mail_id))
            conn.commit()
            log.info("regen.delivered", mail_id=mail_id, mailbox=mailbox.name)
        else:
            log.warning("regen.delivery_failed", mail_id=mail_id, mailbox=mailbox.name)
        return ok
    finally:
        conn.close()


async def main(apply: bool, ids: list[int]) -> None:
    log.info("regen.start", apply=apply, count=len(ids), ids=ids)
    success = 0
    failed = 0
    for mail_id in ids:
        try:
            ok = await _regenerate_and_deliver(mail_id, apply)
            if ok or not apply:
                success += 1
            else:
                failed += 1
        except Exception as exc:
            log.error("regen.exception", mail_id=mail_id, error=str(exc))
            failed += 1
    log.info("regen.done", success=success, failed=failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ids", type=str, required=True, help="IDs séparés par des virgules")
    args = parser.parse_args()
    ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
    asyncio.run(main(args.apply, ids))
