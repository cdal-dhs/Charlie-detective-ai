"""Supprime les brouillons IMAP qui ne contiennent pas de tag EMAIL #xxx.

Usage:
  python -m scripts.cleanup_drafts_without_email_id --dry-run
  python -m scripts.cleanup_drafts_without_email_id --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from email import message_from_bytes
from pathlib import Path

import aioimaplib
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import _find_drafts_folder

log = structlog.get_logger()


async def _cleanup_mailbox(mb, apply: bool) -> tuple[int, int]:
    client = aioimaplib.IMAP4_SSL(host=mb.imap_host, port=mb.imap_port)
    await client.wait_hello_from_server()
    login = await client.login(mb.user, mb.app_password)
    if login.result != "OK":
        log.warning("cleanup.login_failed", mailbox=mb.name)
        return 0, 0

    draft_folder = await _find_drafts_folder(client)
    await client.select(draft_folder)
    _, data = await client.search("ALL")
    uids = data[0].decode().split() if data and data[0] else []

    def _extract_text(raw: bytes) -> str:
        try:
            msg = message_from_bytes(raw)
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            return payload.decode("utf-8", errors="replace")
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        except Exception:
            pass
        return raw.decode("utf-8", errors="replace")

    deleted = 0
    kept = 0
    for uid in uids:
        _, msg_data = await client.fetch(uid, "(RFC822)")
        raw = (
            bytes(msg_data[1])
            if len(msg_data) > 1 and isinstance(msg_data[1], (bytes, bytearray))
            else b""
        )
        text = _extract_text(raw)
        has_email = bool(re.search(r"EMAIL #\d+", text))
        if has_email:
            kept += 1
            continue
        if apply:
            await client.store(uid, "+FLAGS", "\\Deleted")
            log.info("cleanup.deleted", mailbox=mb.name, uid=uid, folder=draft_folder)
            deleted += 1
        else:
            log.info("cleanup.would_delete", mailbox=mb.name, uid=uid, folder=draft_folder)
            deleted += 1

    if apply and deleted:
        await client.expunge()
        log.info("cleanup.expunged", mailbox=mb.name, deleted=deleted, kept=kept)

    await client.logout()
    return deleted, kept


async def main(apply: bool) -> None:
    settings = get_settings()
    total_deleted = 0
    total_kept = 0
    for mb in settings.mailboxes():
        deleted, kept = await _cleanup_mailbox(mb, apply)
        total_deleted += deleted
        total_kept += kept
    log.info("cleanup.done", apply=apply, deleted=total_deleted, kept=total_kept)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply or not args.dry_run
    asyncio.run(main(apply))
