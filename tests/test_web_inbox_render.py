"""Tests de non-régression du rendu cockpit inbox (/app/ et /api/inbox).

v1.25.19 — le bug P0 a été introduit par un désalignement entre le SELECT SQL
et la liste `cols` de `_fetch_mails()` / `_fetch_mails_partial()`. `ai_draft`
recevait la valeur entière de `attachment_count`, ce qui provoquait :

    TypeError: object of type 'int' has no len()

sur `m.ai_draft|length` dans `app/web/templates/app/inbox_rows.html`.

Ces tests créent une DB temporaire avec le vrai schema mail_processed et
vérifient que le template inbox se rend sans 500, avec et sans brouillon,
et que le masque forwarder WP s'applique correctement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.web.app import make_app
from app.web.deps import get_db, require_operator


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER DEFAULT 0,
            draft_sent_at TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            priority TEXT,
            ai_draft TEXT,
            human_draft TEXT,
            reviewed_by INTEGER,
            reviewed_at DATETIME,
            sent_at DATETIME,
            sent_by INTEGER,
            body_preview TEXT,
            body TEXT,
            delivered_at TEXT,
            suggested_subject TEXT,
            UNIQUE(imap_uid, mailbox_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT,
            storage_path TEXT,
            size_bytes INTEGER,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Mail avec brouillon généré (hot — demande_client + high + pending)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "uid1",
            "detective_belgique",
            "Demande de tarif",
            "client@example.com",
            "2026-06-23 10:00:00",
            "demande_client",
            "pending",
            "high",
            "Bonjour, je voudrais un tarif...",
            "Bonjour, je voudrais un tarif...",
            "Cher client, voici nos tarifs...",
            1,
            "2026-06-23 10:01:00",
        ),
    )
    # Mail sans brouillon
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            2,
            "uid2",
            "detective_belgium",
            "Newsletter mensuelle",
            "newsletter@fournisseur.be",
            "2026-06-23 09:00:00",
            "newsletter",
            "approved",
            "low",
            "Notre newsletter de juin...",
            "Notre newsletter de juin...",
            None,
            0,
            "2026-06-23 09:01:00",
        ),
    )
    # Forwarder WP NL sans email client (doit être masqué)
    conn.execute(
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            3,
            "uid3",
            "detective_belgium",
            "Nieuwe aanvraag",
            "wordpress@detectivebelgium.com",
            "2026-06-23 08:00:00",
            "demande_client",
            "pending",
            "high",
            "Achternaam: Dupont\nVoornaam: Jean\nTelefoonnummer: 0477/123456",
            "Achternaam: Dupont\nVoornaam: Jean\nTelefoonnummer: 0477/123456",
            "Cher Jean, je vous appelle...",
            1,
            "2026-06-23 08:01:00",
        ),
    )
    conn.commit()
    conn.close()
    return db


def _db_path_holder(db_path: Path):
    async def _override():
        db = await aiosqlite.connect(str(db_path))
        try:
            yield db
        finally:
            await db.close()

    return _override


@pytest.fixture
def operator_user():
    return {"id": 1, "email": "cdal@digitalhs.biz", "role": "super_admin", "name": "CDAL"}


@pytest.fixture
def client(tmp_path: Path, operator_user):
    db_path = _make_db(tmp_path)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    test_client = TestClient(app)
    test_client._db_path = db_path  # type: ignore[attr-defined]
    return test_client


def test_app_index_renders_inbox_200(client) -> None:
    """Le cockpit /app/ doit s'afficher sans erreur 500."""
    resp = client.get("/app/")
    assert resp.status_code == 200, resp.text[:500]


def test_api_inbox_renders_rows_200(client) -> None:
    """Le fragment HTMX /api/inbox doit retourner les lignes sans 500."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200, resp.text[:500]


def test_inbox_rows_show_draft_badge(client) -> None:
    """Un mail avec ai_draft non vide affiche le badge brouillon dans la colonne Boîte."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    # Le title du badge est plus stable que l'emoji qui peut être encodé différemment.
    assert "Proposition de réponse générée par Charlie" in resp.text
    assert "Demande de tarif" in resp.text


def test_inbox_rows_mask_forwarder_sender(client) -> None:
    """Un forwarder WP sans email client doit afficher NO_EMAIL_IN_THE_FORM."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    assert "NO_EMAIL_IN_THE_FORM" in resp.text
    # L'email technique ne doit pas apparaître dans le rendu cockpit.
    assert "wordpress@detectivebelgium.com" not in resp.text


def test_inbox_rows_no_int_len_crash(client) -> None:
    """Régression P0 : ai_draft ne doit jamais être un int (cela causait |length error)."""
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    # Si l'erreur se reproduisait, FastAPI retournerait du JSON detail 500, pas du HTML.
    assert "Internal Server Error" not in resp.text
    assert "TypeError" not in resp.text
