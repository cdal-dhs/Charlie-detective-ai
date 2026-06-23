"""Supprime des brouillons IMAP ciblés par boîte + UID.

Usage:
  python -m scripts.cleanup_drafts_by_uid --apply --mailbox detective_belgique --uid 1 3
  python -m scripts.cleanup_drafts_by_uid --apply \
      --config detective_belgique:1,3;dpdh_investigations:5,9
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aioimaplib
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import _find_drafts_folder

log = structlog.get_logger()


def _parse_config(s: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for part in s.split(";"):
        if not part.strip():
            continue
        mb, uids = part.split(":", 1)
        result[mb.strip()] = [u.strip() for u in uids.split(",")]
    return result


async def _delete_uids(mb_name: str, uids: list[str], apply: bool) -> None:
    settings = get_settings()
    mb = next((m for m in settings.mailboxes() if m.name == mb_name), None)
    if mb is None:
        log.warning("cleanup.unknown_mailbox", mailbox=mb_name)
        return

    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    await client.wait_hello_from_server()
    login = await client.login(mb.user, mb.app_password)
    if login.result != "OK":
        log.warning("cleanup.login_failed", mailbox=mb_name)
        return

    draft_folder = await _find_drafts_folder(client)
    await client.select(draft_folder)

    for uid in uids:
        if apply:
            await client.store(uid, "+FLAGS", "\\Deleted")
            log.info("cleanup.deleted", mailbox=mb_name, uid=uid, folder=draft_folder)
        else:
            log.info("cleanup.dry_run", mailbox=mb_name, uid=uid, folder=draft_folder)

    if apply:
        await client.expunge()
        log.info("cleanup.expunged", mailbox=mb_name, deleted_count=len(uids))

    await client.logout()


async def main(apply: bool, mailbox: str | None, uids: list[str], config: str | None) -> None:
    targets: dict[str, list[str]] = {}
    if config:
        targets = _parse_config(config)
    elif mailbox and uids:
        targets[mailbox] = uids
    else:
        log.error("cleanup.no_targets", message="Utiliser --config ou --mailbox + --uid")
        return

    for mb_name, uid_list in targets.items():
        await _delete_uids(mb_name, uid_list, apply)

    if not apply:
        log.info("cleanup.dry_run_note", message="Relancer avec --apply pour supprimer.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mailbox", type=str)
    parser.add_argument("--uid", type=str, nargs="+")
    parser.add_argument("--config", type=str, help="mb1:uid1,uid2;mb2:uid3")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.mailbox, args.uid, args.config))
