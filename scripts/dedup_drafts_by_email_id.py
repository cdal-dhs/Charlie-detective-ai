"""Dédoublonne les brouillons IMAP par EMAIL #id : garde l'UID le plus élevé.

Usage:
  python -m scripts.dedup_drafts_by_email_id --dry-run
  python -m scripts.dedup_drafts_by_email_id --apply
  python -m scripts.dedup_drafts_by_email_id --dry-run --mailbox detective_belgique
  python -m scripts.dedup_drafts_by_email_id --apply --skip-mailbox detectives_belgique

v1.27.5 — Robustesse OVH/Infomaniak :
- timeout FETCH étendu (60s au lieu du défaut ~10s) pour gérer les serveurs
  lents (OVH ex5.mail.ovh.net a des timeouts courts sur FETCH massif).
- try/except par UID : si un FETCH timeout sur 1 brouillon, on loggue
  l'erreur et on continue — pas de crash de tout le script.
- filtre --mailbox / --skip-mailbox pour traiter les boîtes une par une
  (cas où OVH crashe FETCH massif : on dédoublonne les 3 Infomaniak
  séparément et on retente OVH ensuite).
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
    # v1.27.5 — pas de timeout constructeur (cause CommandTimeout trop court
    # sur SEARCH ALL d'OVH). Timeout explicite 60s uniquement sur FETCH().
    client = aioimaplib.IMAP4_SSL(host=mb.imap_host, port=mb.imap_port)
    await client.wait_hello_from_server()
    login = await client.login(mb.user, mb.app_password)
    if login.result != "OK":
        log.warning("dedup.login_failed", mailbox=mb.name)
        return 0, 0

    draft_folder = await _find_drafts_folder(client)
    await client.select(draft_folder)
    # v1.27.5 — fallback OVH SEARCH ALL : ex5.mail.ovh.net renvoie parfois
    # `[BADCHARSET (US-ASCII)] The specified charset...` au lieu d'une liste
    # d'UIDs. Le poller gère déjà ça (imap_poller.py:_search_unprocessed) ;
    # ici on retente en SEARCH ALL simple et on valide que la réponse est
    # bien une liste d'UIDs numériques.
    _, data = await client.search("ALL", charset=None)
    raw_uids = data[0].decode().split() if data and data[0] else []
    # Filtre : ne garder que les tokens qui sont des UIDs valides (entiers).
    # Ça élimine `[BADCHARSET`, `(US-ASCII)]`, `The`, `specified`, etc.
    uids = [u for u in raw_uids if u.isdigit()]

    by_email_id: dict[str, list[str]] = defaultdict(list)
    fetch_failures = 0
    for i, uid in enumerate(uids, 1):
        try:
            # v1.27.5 — aioimaplib 2.0.1 n'accepte PAS le kwarg `timeout`
            # sur fetch() (vérifié dans le source). Le timeout serveur par
            # défaut (~10s) peut timeout sur OVH ex5.mail.ovh.net en FETCH
            # massif, mais le try/except permet de continuer sans crasher.
            _, msg_data = await client.fetch(uid, "(RFC822)")
        except Exception as exc:
            # v1.27.5 — OVH timeout sur FETCH massif : on loggue + on continue,
            # pas de crash de tout le script.
            fetch_failures += 1
            log.warning(
                "dedup.fetch_failed",
                mailbox=mb.name,
                uid=uid,
                error=str(exc)[:120],
            )
            continue
        # v1.27.5 — throttle léger entre FETCH : OVH ex5.mail.ovh.net timeout
        # si on FETCH en rafale. 100ms entre chaque = 100 FETCH/s max, bien
        # sous les limites IMAP. Asynchrone donc pas bloquant pour le serveur.
        if i % 5 == 0:
            await asyncio.sleep(0.1)
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

    if fetch_failures:
        log.warning(
            "dedup.fetch_failures_summary",
            mailbox=mb.name,
            failures=fetch_failures,
            total_uids=len(uids),
        )

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
                try:
                    await client.store(uid, "+FLAGS", "\\Deleted")
                    log.info("dedup.deleted", mailbox=mb.name, uid=uid, email_id=email_id, keep=keep)
                    deleted += 1
                except Exception as exc:
                    log.warning(
                        "dedup.store_failed",
                        mailbox=mb.name,
                        uid=uid,
                        email_id=email_id,
                        error=str(exc)[:120],
                    )
            else:
                log.info(
                    "dedup.would_delete", mailbox=mb.name, uid=uid, email_id=email_id, keep=keep
                )
                deleted += 1

    if apply and deleted:
        try:
            await client.expunge()
            log.info("dedup.expunged", mailbox=mb.name, deleted=deleted, kept=kept)
        except Exception as exc:
            log.warning(
                "dedup.expunge_failed",
                mailbox=mb.name,
                error=str(exc)[:120],
            )

    try:
        await client.logout()
    except Exception:
        pass
    return deleted, kept


async def main(apply: bool, only_mailbox: str | None, skip_mailbox: str | None) -> None:
    settings = get_settings()
    total_deleted = 0
    total_kept = 0
    for mb in settings.mailboxes():
        if only_mailbox and mb.name != only_mailbox:
            continue
        if skip_mailbox and mb.name == skip_mailbox:
            log.info("dedup.skip_mailbox", mailbox=mb.name)
            continue
        try:
            deleted, kept = await _dedup_mailbox(mb, apply)
        except Exception as exc:
            # v1.27.5 — ne pas crasher tout le script si une boîte timeout
            # (cas OVH ex5.mail.ovh.net + FETCH massif).
            log.warning(
                "dedup.mailbox_failed",
                mailbox=mb.name,
                error=str(exc)[:200],
            )
            continue
        total_deleted += deleted
        total_kept += kept
    log.info("dedup.done", apply=apply, deleted=total_deleted, kept=total_kept)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Ne traiter que cette boîte (ex: detective_belgique)",
    )
    parser.add_argument(
        "--skip-mailbox",
        default=None,
        help="Sauter cette boîte (ex: detectives_belgique si OVH timeout)",
    )
    args = parser.parse_args()
    apply = args.apply or not args.dry_run
    asyncio.run(main(apply=apply, only_mailbox=args.mailbox, skip_mailbox=args.skip_mailbox))
