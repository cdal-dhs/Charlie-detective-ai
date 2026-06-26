"""Dédoublonne les brouillons IMAP par EMAIL #id : garde l'UID le plus élevé.

Usage:
  python -m scripts.dedup_drafts_by_email_id --dry-run
  python -m scripts.dedup_drafts_by_email_id --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from email import message_from_bytes
from pathlib import Path

import aioimaplib
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import _find_drafts_folder

log = structlog.get_logger()


async def _dedup_mailbox(mb, apply: bool) -> tuple[int, int]:
    client = aioimaplib.IMAP4_SSL(host=mb.imap_host, port=mb.imap_port)
    await client.wait_hello_from_server()
    login = await client.login(mb.user, mb.app_password)
    if login.result != "OK":
        log.warning("dedup.login_failed", mailbox=mb.name)
        return 0, 0

    draft_folder = await _find_drafts_folder(client)
    await client.select(draft_folder)
    _, data = await client.search("ALL")
    uids = data[0].decode().split() if data and data[0] else []

    by_email_id: dict[str, list[str]] = defaultdict(list)
    for uid in uids:
        _, msg_data = await client.fetch(uid, "(RFC822)")
        raw = (
            bytes(msg_data[1])
            if len(msg_data) > 1 and isinstance(msg_data[1], (bytes, bytearray))
            else b""
        )
        try:
            msg = message_from_bytes(raw)
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            body = raw.decode("utf-8", errors="replace")
        m = re.search(r"EMAIL #(\d+)", body)
        if m:
            by_email_id[m.group(1)].append(uid)

    deleted = 0
    kept = 0
    for email_id, uid_list in by_email_id.items():
        if len(uid_list) <= 1:
            kept += 1
            continue
        # Garde l'UID le plus élevé (habituellement le plus récent après expunge)
        sorted_uids = sorted(uid_list, key=int)
        keep = sorted_uids[-1]
        remove = sorted_uids[:-1]
        kept += 1
        for uid in remove:
            if apply:
                await client.store(uid, "+FLAGS", "\\Deleted")
                log.info("dedup.deleted", mailbox=mb.name, uid=uid, email_id=email_id, keep=keep)
                deleted += 1
            else:
                log.info(
                    "dedup.would_delete", mailbox=mb.name, uid=uid, email_id=email_id, keep=keep
                )
                deleted += 1

    if apply and deleted:
        await client.expunge()
        log.info("dedup.expunged", mailbox=mb.name, deleted=deleted, kept=kept)

    await client.logout()
    return deleted, kept


async def main(apply: bool) -> None:
    settings = get_settings()
    total_deleted = 0
    total_kept = 0
    for mb in settings.mailboxes():
        deleted, kept = await _dedup_mailbox(mb, apply)
        total_deleted += deleted
        total_kept += kept
    log.info("dedup.done", apply=apply, deleted=total_deleted, kept=total_kept)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply or not args.dry_run
    asyncio.run(main(apply))
