#!/usr/bin/env python3
"""Ingestion batch des emails sortants (Sent) dans Cerveau2 — un par un pour fiabilité."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from email import message_from_bytes
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import structlog
from aioimaplib import aioimaplib

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.cerveau_client import feed_correspondance
from app.config import get_settings

log = structlog.get_logger()

SENT_FOLDERS = ["Sent Messages", "Sent", "Sent Items"]


def _init_tracking(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cerveau2_ingested (
            message_id TEXT PRIMARY KEY,
            mailbox_name TEXT,
            direction TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def _already_ingested(db_path: Path, message_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM cerveau2_ingested WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_ingested(db_path: Path, message_id: str, mailbox_name: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO cerveau2_ingested (message_id, mailbox_name, direction) VALUES (?, ?, ?)",
        (message_id, mailbox_name, "out"),
    )
    conn.commit()
    conn.close()


def _get_body_text(msg) -> str:
    import html as html_mod
    import re

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    raw = payload.decode("utf-8", errors="replace")
                    return html_mod.unescape(re.sub(r"<[^>]+>", "", raw))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                return html_mod.unescape(re.sub(r"<[^>]+>", "", text))
            return text
    return ""


async def _ingest_mailbox(mb, settings) -> tuple[int, int, int]:
    client = aioimaplib.IMAP4_SSL(mb.imap_host, mb.imap_port)
    await client.wait_hello_from_server()
    await client.login(mb.user, mb.app_password)

    sent_folder = None
    for folder in SENT_FOLDERS:
        status = await client.select(folder)
        if status.result == "OK":
            sent_folder = folder
            break

    if not sent_folder:
        log.warning("sent.not_found", mailbox=mb.name)
        await client.logout()
        return 0, 0, 0

    log.info("sent.folder_selected", mailbox=mb.name, folder=sent_folder)

    search = await client.search("ALL")
    uids = search.lines[0].split() if search.lines else []
    total = len(uids)
    log.info("sent.total", mailbox=mb.name, count=total)

    success = 0
    skipped = 0
    errors = 0

    for idx, uid_bytes in enumerate(uids):
        uid = uid_bytes.decode() if isinstance(uid_bytes, bytes) else uid_bytes

        fetch_resp = await client.fetch(uid, "RFC822")
        if fetch_resp.result != "OK" or len(fetch_resp.lines) < 2:
            errors += 1
            continue

        rfc822_bytes = bytes(fetch_resp.lines[1])
        msg = message_from_bytes(rfc822_bytes)

        message_id = msg.get("Message-ID", "")
        if not message_id:
            message_id = f"{mb.name}_sent_{uid}"

        if _already_ingested(settings.db_agent_state, message_id):
            skipped += 1
            continue

        sender_raw = msg.get("From", "")
        dest_raw = msg.get("To", "")
        subject = msg.get("Subject", "")
        date_str = msg.get("Date", "")
        body = _get_body_text(msg)

        heure = ""
        date_clean = ""
        try:
            dt = parsedate_to_datetime(date_str)
            date_clean = dt.strftime("%Y-%m-%d")
            heure = dt.strftime("%H:%M")
        except Exception:
            date_clean = date_str[:10] if date_str else ""

        try:
            await feed_correspondance(
                message_id=message_id,
                direction="out",
                date=date_clean,
                heure=heure,
                expediteur=parseaddr(sender_raw)[1] or sender_raw,
                destinataire=parseaddr(dest_raw)[1] or dest_raw,
                objet=subject,
                body=body,
                marque=mb.cerveau2_marque,
                dossier_id="",
                categorie="demande_client",
                zone="jaune",
                langue=mb.default_lang,
                priorite="normal",
                base_url=settings.cerveau2_base_url,
                api_secret=settings.cerveau2_api_secret,
            )
            _mark_ingested(settings.db_agent_state, message_id, mb.name)
            success += 1
        except Exception as e:
            log.warning("sent.ingest_failed", mailbox=mb.name, uid=uid, error=str(e))
            errors += 1

        if (idx + 1) % 50 == 0:
            log.info(
                "sent.progress",
                mailbox=mb.name,
                done=idx + 1,
                total=total,
                success=success,
                skipped=skipped,
                errors=errors,
            )

    await client.logout()
    log.info("sent.mailbox_done", mailbox=mb.name, success=success, skipped=skipped, errors=errors)
    return success, skipped, errors


async def main():
    settings = get_settings()
    _init_tracking(settings.db_agent_state)

    total_success = 0
    total_skipped = 0
    total_errors = 0

    for mb in settings.mailboxes():
        s, sk, e = await _ingest_mailbox(mb, settings)
        total_success += s
        total_skipped += sk
        total_errors += e

    log.info(
        "sent.ingestion_complete",
        total_success=total_success,
        total_skipped=total_skipped,
        total_errors=total_errors,
    )
    print("\n=== INGESTION TERMINÉE ===")
    print(f"Succès : {total_success}")
    print(f"Déjà présents : {total_skipped}")
    print(f"Erreurs : {total_errors}")


if __name__ == "__main__":
    asyncio.run(main())
