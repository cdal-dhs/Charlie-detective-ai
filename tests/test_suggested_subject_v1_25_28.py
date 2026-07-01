"""Tests v1.25.28 — persistance du suggested_subject (fix sujet IMAP moche).

Contexte (#643) : le livreur backfill (deliver_pending_drafts) reconstruisait
le GenerationResult SANS suggested_subject → append_draft retombait sur
incoming.subject (template WP absurde / tag [NO_EMAIL_IN_THE_FORM]) au lieu
du sujet lisible (ex. "Investigation successorale — Philippe Boeteman").

Fix : persister suggested_subject en DB à la génération (poller _persist +
backfill _update_db) et le relire dans le livreur (_fetch_pending).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from app.workers.imap_poller import _persist
from scripts.backfill_reclassify import _update_db
from scripts.deliver_pending_drafts import _ensure_column, _fetch_pending


def _db_with_cols(tmp_path: Path, cols_sql: str) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        f"""
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT, mailbox_name TEXT, subject TEXT, sender TEXT,
            received_at TEXT, category TEXT, draft_generated INTEGER,
            body_preview TEXT, body TEXT, ai_draft TEXT, status TEXT,
            priority TEXT, reply_to TEXT,
            {cols_sql}
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    return db


# --- 1. poller _persist : stocke suggested_subject ---


def test_persist_stores_suggested_subject(tmp_path: Path) -> None:
    """Le poller doit persister gen.suggested_subject à l'INSERT (nouveau mail)."""
    db = _db_with_cols(tmp_path, "suggested_subject TEXT, message_id TEXT, in_reply_to TEXT, \"references\" TEXT, dossier_id TEXT, thread_id TEXT, thread_subject TEXT, ")
    mail_id = _persist(
        db_path=db,
        imap_uid="u643",
        mailbox_name="detective_belgique",
        subject="Nouveau Message De Détective privé Belgique - Prenons contact",
        sender="phboeteman@hotmail.com",
        received_at="d",
        category="demande_client",
        draft_generated=1,
        ai_draft="BROUILLON SUCCESSION",
        suggested_subject="Investigation successorale — Philippe Boeteman",
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ai_draft, suggested_subject FROM mail_processed WHERE id = ?",
        (mail_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "BROUILLON SUCCESSION"
    assert row[1] == "Investigation successorale — Philippe Boeteman"


def test_persist_update_enriches_suggested_subject(tmp_path: Path) -> None:
    """Sur un mail existant (UPDATE), suggested_subject est enrichi via COALESCE
    (n'écrase pas une valeur déjà présente)."""
    db = _db_with_cols(tmp_path, "suggested_subject TEXT, message_id TEXT, in_reply_to TEXT, \"references\" TEXT, dossier_id TEXT, thread_id TEXT, thread_subject TEXT, ")
    # 1er passage : INSERT avec suggested_subject
    mail_id = _persist(
        db_path=db,
        imap_uid="u643",
        mailbox_name="detective_belgique",
        subject="S",
        sender="c@x.com",
        received_at="d",
        category="demande_client",
        draft_generated=1,
        suggested_subject="Investigation successorale — Philippe Boeteman",
    )
    # 2e passage : UPDATE avec suggested_subject vide → ne doit pas écraser
    _persist(
        db_path=db,
        imap_uid="u643",
        mailbox_name="detective_belgique",
        subject="S",
        sender="c@x.com",
        received_at="d",
        category="demande_client",
        draft_generated=1,
        suggested_subject="",
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT suggested_subject FROM mail_processed WHERE id = ?", (mail_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "Investigation successorale — Philippe Boeteman"


# --- 2. backfill _update_db : persiste suggested_subject ---


def test_update_db_persists_suggested_subject(tmp_path: Path) -> None:
    """Le backfill _update_db doit écrire suggested_subject (COALESCE, n'écrase pas)."""
    db = _db_with_cols(tmp_path, "suggested_subject TEXT, message_id TEXT, in_reply_to TEXT, \"references\" TEXT, dossier_id TEXT, thread_id TEXT, thread_subject TEXT, ")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO mail_processed (id, category, draft_generated) VALUES (643, 'phishing', 0)"
    )
    conn.commit()
    conn.close()

    _update_db(
        db_path=db,
        mail_id=643,
        new_category="demande_client",
        draft="BROUILLON SUCCESSION",
        apply=True,
        suggested_subject="Investigation successorale — Philippe Boeteman",
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT category, ai_draft, suggested_subject FROM mail_processed WHERE id = 643"
    ).fetchone()
    conn.close()
    assert row[0] == "demande_client"
    assert row[1] == "BROUILLON SUCCESSION"
    assert row[2] == "Investigation successorale — Philippe Boeteman"


def test_update_db_does_not_overwrite_existing_suggested_subject(tmp_path: Path) -> None:
    """_update_db avec suggested_subject vide ne doit pas écraser une valeur existante."""
    db = _db_with_cols(tmp_path, "suggested_subject TEXT, message_id TEXT, in_reply_to TEXT, \"references\" TEXT, dossier_id TEXT, thread_id TEXT, thread_subject TEXT, ")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO mail_processed (id, category, draft_generated, suggested_subject) "
        "VALUES (643, 'phishing', 0, 'Ancien sujet lisible')"
    )
    conn.commit()
    conn.close()

    _update_db(
        db_path=db,
        mail_id=643,
        new_category="demande_client",
        draft="NOUVEAU BROUILLON",
        apply=True,
        suggested_subject="",
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT ai_draft, suggested_subject FROM mail_processed WHERE id = 643"
    ).fetchone()
    conn.close()
    assert row[0] == "NOUVEAU BROUILLON"
    # COALESCE(NULLIF('', ''), suggested_subject) → garde l'ancien.
    assert row[1] == "Ancien sujet lisible"


# --- 3. livreur _fetch_pending : lit suggested_subject (+ _ensure_column) ---


def test_ensure_column_adds_suggested_subject(tmp_path: Path) -> None:
    """_ensure_column doit ajouter delivered_at ET suggested_subject idempotemment."""
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY, imap_uid TEXT, mailbox_name TEXT,
            subject TEXT, sender TEXT, received_at TEXT, category TEXT,
            draft_generated INTEGER, body_preview TEXT, body TEXT,
            ai_draft TEXT, status TEXT, priority TEXT, reply_to TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    asyncio.run(_ensure_column(db))

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mail_processed)").fetchall()}
    conn.close()
    assert "delivered_at" in cols
    assert "suggested_subject" in cols


def test_fetch_pending_returns_suggested_subject(tmp_path: Path) -> None:
    """Le livreur doit lire suggested_subject pour reconstruire le GenerationResult
    (sans ça, append_draft retombe sur incoming.subject → sujet IMAP moche)."""
    db = _db_with_cols(tmp_path, "suggested_subject TEXT, delivered_at TEXT, ")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO mail_processed (id, imap_uid, mailbox_name, subject, sender, "
        "received_at, category, draft_generated, ai_draft, body, suggested_subject, "
        "delivered_at) "
        "VALUES (643, 'u643', 'detective_belgique', 'Sujet original moche', "
        "'phboeteman@hotmail.com', 'd', 'demande_client', 1, 'BROUILLON', 'body', "
        "'Investigation successorale — Philippe Boeteman', NULL)"
    )
    conn.commit()
    conn.close()

    pending = asyncio.run(_fetch_pending(db, only_id=643))
    assert len(pending) == 1
    assert pending[0]["suggested_subject"] == "Investigation successorale — Philippe Boeteman"
