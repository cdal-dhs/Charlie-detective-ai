"""Tests v1.25.28 — le cockpit affiche le suggested_subject (lisible) en priorité
sur le sujet original (template WP absurde / tag [NO_EMAIL_IN_THE_FORM]). Cf. #643.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

from app.web.app_routes import _fetch_mail, _fetch_mails


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT, mailbox_name TEXT, subject TEXT, sender TEXT,
            received_at TEXT, category TEXT, draft_generated INTEGER,
            processed_at TEXT, status TEXT, priority TEXT, ai_draft TEXT,
            human_draft TEXT, reviewed_by INTEGER, reviewed_at DATETIME,
            sent_at DATETIME, sent_by INTEGER,
            body_preview TEXT, body TEXT, reply_to TEXT, suggested_subject TEXT,
            delivered_at TEXT,
            -- v1.29.0 — threading columns
            message_id TEXT, in_reply_to TEXT, "references" TEXT,
            dossier_id TEXT, thread_id TEXT, thread_subject TEXT,
            -- v1.29.0.6 — dedup column
            duplicate_of INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER, filename TEXT, storage_path TEXT,
            size_bytes INTEGER, extracted_text_preview TEXT, created_at TEXT
        )
        """
    )
    # Mail #643-like : demande_client hot avec sujet original moche + suggested_subject lisible.
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " draft_generated, processed_at, status, priority, ai_draft, body_preview, "
        " body, suggested_subject) "
        "VALUES (643, 'u643', 'detective_belgique', "
        " 'Nouveau Message De Détective privé Belgique - Prenons contact [NO_EMAIL_IN_THE_FORM]', "
        " 'phboeteman@hotmail.com', '2026-06-24', 'demande_client', 1, "
        " '2026-06-24 18:00:00', 'pending', 'high', 'BROUILLON SUCCESSION', 'preview', "
        " 'body', 'Investigation successorale — Philippe Boeteman')"
    )
    # Mail contrôle : pas de suggested_subject → on garde le sujet original.
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " draft_generated, processed_at, status, priority, ai_draft, body_preview, body) "
        "VALUES (700, 'u700', 'detective_belgique', 'Demande de filature — claire', "
        " 'c@x.com', '2026-06-24', 'demande_client', 1, '2026-06-24 18:00:00', "
        " 'pending', 'high', 'BROUILLON', 'p', 'b')"
    )
    conn.commit()
    conn.close()
    return db


def test_inbox_displays_suggested_subject_over_original(tmp_path: Path) -> None:
    """L'inbox doit afficher le suggested_subject (lisible) au lieu du sujet original
    moche (template WP + tag [NO_EMAIL_IN_THE_FORM])."""
    db_path = _make_db(tmp_path)

    async def go() -> tuple[list[dict], list[dict]]:
        async with aiosqlite.connect(db_path) as db:
            return await _fetch_mails(
                db, boxes=None, category=None, status=None, priority=None, q=None
            )

    hot, other = asyncio.run(go())
    all_mails = {m["id"]: m for m in hot + other}

    # #643 : suggested_subject affiché à la place du sujet original moche.
    assert "Investigation successorale — Philippe Boeteman" in all_mails[643]["subject"]
    assert "[NO_EMAIL_IN_THE_FORM]" not in all_mails[643]["subject"]
    assert "Prenons contact" not in all_mails[643]["subject"]

    # #700 (contrôle, pas de suggested_subject) : sujet original inchangé.
    assert all_mails[700]["subject"] == "Demande de filature — claire"


def test_conversation_displays_suggested_subject(tmp_path: Path) -> None:
    """La page conversation doit afficher le suggested_subject (titre + header)."""
    db_path = _make_db(tmp_path)

    async def go() -> dict | None:
        async with aiosqlite.connect(db_path) as db:
            return await _fetch_mail(db, 643)

    mail = asyncio.run(go())
    assert mail is not None
    assert "Investigation successorale — Philippe Boeteman" in mail["subject"]
    assert "[NO_EMAIL_IN_THE_FORM]" not in mail["subject"]


def test_conversation_keeps_original_when_no_suggested(tmp_path: Path) -> None:
    """Sans suggested_subject, la conversation garde le sujet original."""
    db_path = _make_db(tmp_path)

    async def go() -> dict | None:
        async with aiosqlite.connect(db_path) as db:
            return await _fetch_mail(db, 700)

    mail = asyncio.run(go())
    assert mail is not None
    assert mail["subject"] == "Demande de filature — claire"
