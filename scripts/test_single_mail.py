"""Test one-shot : traite exactement 1 mail non-flaggé et envoie le brouillon via Resend.

Usage : venv/bin/python -m scripts.test_single_mail [mailbox_name]
"""

import asyncio
import sys

import structlog

from app.config import get_settings
from app.workers.imap_poller import _process_single_mail
from aioimaplib import aioimaplib

log = structlog.get_logger()


async def main() -> int:
    settings = get_settings()
    target_name = sys.argv[1] if len(sys.argv) > 1 else settings.mailbox_1_name

    mailbox = None
    for mb in settings.mailboxes():
        if mb.name == target_name:
            mailbox = mb
            break
    if mailbox is None:
        print(f"Boîte inconnue : {target_name}")
        return 1

    log.info("test.start", mailbox=mailbox.name)

    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    await client.wait_hello_from_server()
    await client.login(mailbox.user, mailbox.app_password)

    try:
        select_resp = await client.select("INBOX")
        if select_resp.result != "OK":
            raise RuntimeError(f"SELECT failed: {select_resp}")

        search_resp = await client.search("UNKEYWORD $AgentProcessed")
        if search_resp.result != "OK":
            raise RuntimeError(f"SEARCH failed: {search_resp}")

        uids = search_resp.lines[0].split() if search_resp.lines else []
        log.info("test.found", count=len(uids))

        if not uids:
            print("Aucun mail à traiter.")
            return 0

        # Traiter uniquement le PREMIER mail trouvé
        uid = uids[0].decode()
        print(f"Traitement du mail UID={uid}...")
        await _process_single_mail(client, uid, mailbox)
        print(f"Mail UID={uid} traité et brouillon envoyé.")

        await client.logout()
        return 0
    finally:
        pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
