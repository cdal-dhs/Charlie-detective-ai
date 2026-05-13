"""Simulation TRIAL : scan les mails non-flaggés, filtre les internes, et te propose de choisir.

Usage : venv/bin/python -m scripts.simulate_trial [mailbox_name]
"""

import asyncio
import sys

import structlog

from app.config import get_settings
from app.workers.imap_poller import _process_single_mail, _decode_header
from aioimaplib import aioimaplib

log = structlog.get_logger()


# Adresses internes du cabinet à exclure
_INTERNAL_DOMAINS = {
    "detectivebelgique.be",
    "detectivebelgium.com",
    "dpdhuinvestigations.be",
    "digitalhs.biz",
}


def _is_internal(sender: str) -> bool:
    s = sender.lower()
    return any(d in s for d in _INTERNAL_DOMAINS)


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

    log.info("simulate.start", mailbox=mailbox.name)

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

        all_uids = search_resp.lines[0].split() if search_resp.lines else []
        log.info("simulate.found_total", count=len(all_uids))

        if not all_uids:
            print("Aucun mail à traiter.")
            return 0

        # Scanner les 30 premiers pour trouver des candidats
        candidates = []
        max_scan = min(30, len(all_uids))
        print(f"\nScan des {max_scan} premiers mails non-flaggés...\n")
        print(f"{'UID':>6} | {'Expéditeur':<40} | {'Sujet'}")
        print("-" * 100)

        for i in range(max_scan):
            uid_bytes = all_uids[i]
            uid = uid_bytes.decode()

            try:
                fetch_resp = await client.fetch(uid, "RFC822.HEADER")
                if fetch_resp.result != "OK" or len(fetch_resp.lines) < 2:
                    continue

                from email import message_from_bytes
                msg = message_from_bytes(fetch_resp.lines[1])
                sender_raw = msg.get("From", "")
                subject_raw = msg.get("Subject", "")
                sender = _decode_header(sender_raw)
                subject = _decode_header(subject_raw)
                is_int = _is_internal(sender)

                flag = "[INT]" if is_int else "[EXT]"
                print(f"{uid:>6} | {flag} {sender[:36]:<36} | {subject[:50]}")

                if not is_int:
                    candidates.append((uid, sender, subject))
            except Exception:
                continue

        print("-" * 100)
        print(f"\n{len(candidates)} mail(s) externe(s) trouvé(s) parmi les {max_scan} scannés.\n")

        if not candidates:
            print("Aucun mail externe trouvé dans les 30 premiers. Essaye un autre mailbox ou augmente le scan.")
            return 0

        # Proposer les 5 premiers candidats
        for idx, (uid, sender, subject) in enumerate(candidates[:5], 1):
            print(f"  {idx}. UID={uid} | {sender} | {subject}")

        auto = "--auto" in sys.argv
        uid_arg = None
        for arg in sys.argv[1:]:
            if arg.startswith("--uid="):
                uid_arg = arg.split("=", 1)[1]
                break

        if uid_arg:
            selected_uid = uid_arg
            print(f"\n[MODE UID] Traitement du mail UID={selected_uid}.")
        elif auto:
            selected_idx = 0
            selected_uid, selected_sender, selected_subject = candidates[selected_idx]
            print(f"\n[MODE AUTO] Sélection du candidat : UID={selected_uid} | {selected_sender} | {selected_subject}")
        else:
            choice = input("\nQuel numéro traiter ? (1-5, ou ENTREE pour le 1er) : ").strip()
            if not choice:
                choice = "1"
            try:
                selected_idx = int(choice) - 1
                selected_uid, selected_sender, selected_subject = candidates[selected_idx]
            except (ValueError, IndexError):
                print("Choix invalide.")
                return 1

        print(f"\n>>> Traitement du mail UID={selected_uid}...")
        await _process_single_mail(client, selected_uid, mailbox)
        print(f">>> Mail UID={selected_uid} traité et brouillon TRIAL envoyé à {settings.draft_recipient}")

        await client.logout()
        return 0
    finally:
        pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
