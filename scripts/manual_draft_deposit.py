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
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv("/opt/DETECTIVE/.env.production")

DB_PATH = "/opt/DETECTIVE/data/agent_state.db"
IMAP_HOST = os.getenv("IMAP_HOST", "mail.infomaniak.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))

MAILBOXES = {
    "detective_belgique": {
        "user": os.getenv("MAILBOX_1_USER"),
        "password": os.getenv("MAILBOX_1_APP_PASSWORD"),
    },
    "detective_belgium": {
        "user": os.getenv("MAILBOX_2_USER"),
        "password": os.getenv("MAILBOX_2_APP_PASSWORD"),
    },
    "dpdh_investigations": {
        "user": os.getenv("MAILBOX_3_USER"),
        "password": os.getenv("MAILBOX_3_APP_PASSWORD"),
    },
}

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    placeholders = ",".join("?" * len(TARGET_IDS))
    cur.execute(
        f"SELECT id, mailbox_name, subject, sender, ai_draft "  # noqa: S608
        f"FROM mail_processed WHERE id IN ({placeholders})",
        TARGET_IDS,
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("Aucun email trouvé pour les IDs demandés.")
        return

    for row in rows:
        mail_id = row["id"]
        mailbox = row["mailbox_name"]
        subject = row["subject"]
        sender = row["sender"]
        draft = row["ai_draft"]

        if not draft:
            print(f"SKIP {mail_id}: pas de ai_draft en base.")
            continue

        cfg = MAILBOXES.get(mailbox)
        if not cfg or not cfg["user"] or not cfg["password"]:
            print(f"SKIP {mail_id}: config IMAP manquante pour {mailbox}.")
            continue

        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(cfg["user"], cfg["password"])

            drafts_folder = _find_drafts_folder(imap)
            if not drafts_folder:
                print(f"SKIP {mail_id}: dossier Drafts introuvable.")
                imap.logout()
                continue

            msg = email.message.EmailMessage()
            msg["From"] = cfg["user"]
            msg["To"] = sender
            msg["Subject"] = f"PROPOSITION DE REPONSE EMAIL N° {mail_id} / {subject}"
            msg.set_content(draft)

            status, _ = imap.append(
                drafts_folder, r"\Draft", None, msg.as_bytes()
            )
            if status == "OK":
                print(
                    f"OK {mail_id} -> {drafts_folder} ({mailbox}) | "
                    f"Sujet: {msg['Subject']}"
                )
            else:
                print(f"FAIL {mail_id}: IMAP APPEND status={status}")

            imap.logout()
        except Exception as exc:
            print(f"FAIL {mail_id}: {exc}")


if __name__ == "__main__":
    main()
