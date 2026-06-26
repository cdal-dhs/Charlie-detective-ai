#!/usr/bin/env python3
"""Dépose manuellement des brouillons IMAP pour une liste d'emails déjà traités.

Usage (sur le VPS) :
    python3 /opt/DETECTIVE/scripts/manual_draft_deposit.py

Le script lit la DB agent_state.db, récupère le ai_draft existant pour chaque mail,
et fait un IMAP APPEND dans les Drafts de la boîte source avec le sujet demandé :
    PROPOSITION DE REPONSE EMAIL N° {id} / {subject original}
"""

import email.message
import imaplib
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings

DB_PATH = "/opt/DETECTIVE/data/agent_state.db"

TARGET_IDS = [121, 120, 101, 98, 91, 89, 83]

_DRAFT_CANDIDATES = ["Drafts", "INBOX.Drafts", "Brouillons", "INBOX.Brouillons"]


def _find_drafts_folder(imap: imaplib.IMAP4_SSL) -> str | None:
    """Trouve le nom du dossier Drafts via LIST."""
    status, folders = imap.list()
    if status != "OK" or not folders:
        return None

    folder_names: list[str] = []
    for line in folders:
        if not line:
            continue
        decoded = line.decode("utf-8", errors="replace")
        # Dernier segment entre guillemets = nom du dossier
        if '"' in decoded:
            name = decoded.split('"')[-2]
            folder_names.append(name)

    # Match exact prioritaire
    for candidate in _DRAFT_CANDIDATES:
        for name in folder_names:
            if name.lower() == candidate.lower():
                return name

    # Fallback : contient draft/brouillon
    for name in folder_names:
        lowered = name.lower()
        if "draft" in lowered or "brouillon" in lowered:
            return name

    return None


def main() -> None:
    load_dotenv("/opt/DETECTIVE/.env.production")
    settings = get_settings()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    placeholders = ",".join("?" * len(TARGET_IDS))
    cur.execute(
        "SELECT id, mailbox_name, subject, sender, ai_draft "
        f"FROM mail_processed WHERE id IN ({placeholders})",
        TARGET_IDS,
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("Aucun email trouvé pour les IDs demandés.")
        return

    mailboxes_by_name = {mb.name: mb for mb in settings.mailboxes()}

    for row in rows:
        mail_id = row["id"]
        mailbox_name = row["mailbox_name"]
        subject = row["subject"]
        sender = row["sender"]
        draft = row["ai_draft"]

        if not draft:
            print(f"SKIP {mail_id}: pas de ai_draft en base.")
            continue

        mb = mailboxes_by_name.get(mailbox_name)
        if not mb or not mb.user or not mb.app_password:
            print(f"SKIP {mail_id}: config IMAP manquante pour {mailbox_name}.")
            continue

        try:
            imap = imaplib.IMAP4_SSL(mb.imap_host, mb.imap_port)
            imap.login(mb.user, mb.app_password)

            drafts_folder = _find_drafts_folder(imap)
            if not drafts_folder:
                print(f"SKIP {mail_id}: dossier Drafts introuvable.")
                imap.logout()
                continue

            msg = email.message.EmailMessage()
            msg["From"] = mb.user
            msg["To"] = sender
            msg["Subject"] = f"PROPOSITION DE REPONSE EMAIL N° {mail_id} / {subject}"
            msg.set_content(draft)

            status, _ = imap.append(
                drafts_folder, r"\Draft", None, msg.as_bytes()
            )
            if status == "OK":
                print(
                    f"OK {mail_id} -> {drafts_folder} ({mailbox_name}) | "
                    f"Sujet: {msg['Subject']}"
                )
            else:
                print(f"FAIL {mail_id}: IMAP APPEND status={status}")

            imap.logout()
        except Exception as exc:
            print(f"FAIL {mail_id}: {exc}")


if __name__ == "__main__":
    main()
