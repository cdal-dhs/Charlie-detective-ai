"""Supprime les brouillons dans Drafts IMAP pour les mails dont
`received_at` est antérieur à une date seuil (défaut: 2026-01-02).

Contexte : suite au backfill + deliver v1.22.1/1.22.2, Charlie a déposé
153 brouillons dans les Drafts des 4 boîtes Infomaniak. Daniel veut
archiver/nettoyer les vieux brouillons pour ne pas avoir une liste
infernale à parcourir dans sa boîte mail.

Ce script :
  1. Liste les mails avec delivered_at IS NOT NULL + received_at < cutoff
  2. Pour chaque mail, ouvre une connexion IMAP, va dans Drafts
  3. Cherche le brouillon par subject (préfixe 'DEMANDE D'Approbation')
  4. Le marque \\Deleted + EXPUNGE
  5. Met delivered_at à NULL (ou un timestamp d'expunge) en DB

Usage :
  python -m scripts.cleanup_old_drafts                      # dry-run par défaut
  python -m scripts.cleanup_old_drafts --apply              # supprime vraiment
  python -m scripts.cleanup_old_drafts --cutoff 2026-01-02  # change la date
  python -m scripts.cleanup_old_drafts --mailbox detective_belgique
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import aioimaplib
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings

log = structlog.get_logger()


def _parse_received_at(text: str | None) -> datetime | None:
    """Parse 'Wed, 5 Feb 2025 12:16:58 +0100' en datetime."""
    if not text:
        return None
    m = re.match(r"[A-Za-z]{3},\s+(\d+)\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})", text)
    if not m:
        return None
    day, mon_s, year, hh, mm, ss = m.groups()
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    try:
        return datetime(int(year), months[mon_s], int(day), int(hh), int(mm), int(ss))
    except (KeyError, ValueError):
        return None


def _find_mailbox(name: str) -> object | None:
    for mb in get_settings().mailboxes():
        if mb.name == name:
            return mb
    return None


async def _fetch_candidates(db_path, cutoff_date: str, mailbox_filter: str | None) -> list[dict]:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT id, mailbox_name, sender, subject, received_at, delivered_at
            FROM mail_processed
            WHERE delivered_at IS NOT NULL
        """
        params: list = []
        if mailbox_filter:
            sql += " AND mailbox_name = ?"
            params.append(mailbox_filter)
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    cutoff = datetime.fromisoformat(cutoff_date)
    result = []
    for r in rows:
        d = _parse_received_at(r["received_at"])
        if d and d < cutoff:
            r["_parsed"] = d
            result.append(r)
    return result


async def _delete_draft_in_imap(mailbox, subject_marker: str) -> tuple[bool, str]:
    """Cherche et supprime le brouillon dans Drafts. Retourne (ok, info)."""
    client = aioimaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
    try:
        await client.wait_hello_from_server()
        login_resp = await client.login(mailbox.user, mailbox.app_password)
        if login_resp.result != "OK":
            return False, f"login_failed: {login_resp.result}"

        # SELECT probe (v1.21.9 : Infomaniak refuse LIST)
        drafts_folder = None
        for candidate in ["Drafts", "Brouillons", "INBOX.Drafts", "INBOX.Brouillons", "Draft"]:
            sel = await client.select(candidate)
            if sel.result == "OK":
                drafts_folder = candidate
                break

        if not drafts_folder:
            return False, "no_drafts_folder"

        # Le subject du brouillon a le préfixe DEMANDE D'Approbation
        # On sanitize comme dans deliver_pending_drafts
        clean_subject = subject_marker.replace("\r", " ").replace("\n", " ").strip()
        # Le subject du brouillon stocké est l'original ; le APPEND ajoute le préfixe.
        # On cherche le subject complet reconstitué.
        full_subject = f"DEMANDE D'Approbation - Reponse Demande Client : {clean_subject}"

        # SEARCH SUBJECT — comme dans _verify_draft_present
        search_resp = await client.search(f'SUBJECT "{full_subject}"')
        if search_resp.result != "OK":
            return False, "search_failed"

        # lines typique : [b'1 2 3'] ou [b'']
        ids_to_delete = []
        for line in search_resp.lines or []:
            if line.strip():
                ids_to_delete.extend(line.decode().split())

        if not ids_to_delete:
            return False, "not_found_in_drafts"

        # Marque \Deleted + EXPUNGE
        for msg_id in ids_to_delete:
            await client.store(msg_id, "+FLAGS", r"\Deleted")
        await client.expunge()

        return True, f"deleted_{len(ids_to_delete)}"

    except Exception as exc:
        return False, f"exception: {exc}"
    finally:
        with __import__("contextlib").suppress(Exception):
            await client.logout()


async def _mark_unset_delivered(db_path, mail_id: int) -> None:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE mail_processed SET delivered_at = NULL WHERE id = ?",
            (mail_id,),
        )
        await db.commit()


async def main(apply: bool, cutoff: str, mailbox_filter: str | None) -> None:
    settings = get_settings()
    log.info(
        "cleanup.start",
        apply=apply,
        cutoff=cutoff,
        mailbox=mailbox_filter,
        db=str(settings.db_agent_state),
    )

    candidates = await _fetch_candidates(settings.db_agent_state, cutoff, mailbox_filter)
    log.info("cleanup.candidates", count=len(candidates))

    deleted = 0
    failed = 0
    skipped = 0

    for i, mail in enumerate(candidates, 1):
        mailbox = _find_mailbox(mail["mailbox_name"])
        if mailbox is None:
            log.warning(
                "cleanup.no_mailbox",
                mail_id=mail["id"],
                mailbox_name=mail["mailbox_name"],
            )
            skipped += 1
            continue

        if not apply:
            log.info(
                "cleanup.dry_run_would_delete",
                mail_id=mail["id"],
                mailbox=mail["mailbox_name"],
                received_at=mail["received_at"][:16],
                subject=mail["subject"][:50],
            )
            continue

        ok, info = await _delete_draft_in_imap(mailbox, mail["subject"] or "")
        if ok:
            await _mark_unset_delivered(settings.db_agent_state, mail["id"])
            deleted += 1
            log.info(
                "cleanup.ok",
                mail_id=mail["id"],
                mailbox=mail["mailbox_name"],
                info=info,
            )
        else:
            failed += 1
            log.warning(
                "cleanup.failed",
                mail_id=mail["id"],
                mailbox=mail["mailbox_name"],
                info=info,
            )

        if i % 10 == 0:
            log.info("cleanup.progress", processed=i, total=len(candidates))

    log.info(
        "cleanup.done",
        apply=apply,
        candidates=len(candidates),
        deleted=deleted,
        failed=failed,
        skipped=skipped,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Nettoie les brouillons IMAP Drafts < cutoff (v1.22.2)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Supprime vraiment les brouillons (sinon dry-run)",
    )
    parser.add_argument(
        "--cutoff",
        default="2026-01-02",
        help="Date ISO cutoff (defaut: 2026-01-02)",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Filtre sur une seule boîte (ex: detective_belgique)",
    )
    args = parser.parse_args()

    asyncio.run(main(apply=args.apply, cutoff=args.cutoff, mailbox_filter=args.mailbox))
