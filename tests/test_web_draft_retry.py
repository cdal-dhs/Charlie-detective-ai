"""Tests de l'endpoint retry/generate du cockpit (app/web/api.py).

v1.25.2 — vérifie que la (re)génération reclassifie AVANT de générer le brouillon,
pour qu'un mail mal classé (ex: #614 resté phishing) reçoive le brouillon
déterministe du builder (illegal_refusal) au lieu d'un brouillon LLM inadapté.
Et qu'aucun brouillon n'est généré pour une catégorie hors draft_categories.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.web.app import make_app
from app.web.deps import get_db, require_operator


def _make_db(tmp_path: Path, mail_row: tuple) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY,
            imap_uid TEXT,
            mailbox_name TEXT,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            status TEXT,
            priority TEXT,
            body_preview TEXT,
            body TEXT,
            ai_draft TEXT,
            draft_generated INTEGER,
            delivered_at TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT
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
        "INSERT INTO mail_processed "
        "(id, imap_uid, mailbox_name, subject, sender, received_at, category, "
        " status, priority, body_preview, body, ai_draft, draft_generated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        mail_row,
    )
    conn.commit()
    conn.close()
    return db


def _db_path_holder(db_path: Path):
    """Retourne un override async-gen de get_db pointant vers la DB temp."""
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
def client(tmp_path: Path, operator_user, monkeypatch):
    # Une DB avec #614 classé phishing + un ancien brouillon LLM inadapté.
    db_path = _make_db(
        tmp_path,
        (
            614, "uid614", "detective_belgique",
            "іtѕⅿе-Bеvеіlіngѕmеldіng", "serge@example.com", "2026-06-22",
            "phishing", "pending", "high",
            "J'aimerais prouver l'infidélité... combien ça coûte ? merci Serge M",
            "J'aimerais prouver l'infidélité... combien ça coûte ? merci Serge M",
            "ANCIEN BROUILLON LLM INADAPTE", 1,
        ),
    )
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    # On expose db_path via le client pour les assertions post-requête.
    client = TestClient(app)
    client._db_path = db_path  # type: ignore[attr-defined]
    return client


def test_retry_reclassifies_before_generating(client, monkeypatch):
    """#614 classé phishing + retry → reclassifié demande_client + brouillon généré."""
    reclass_called = {"n": 0}

    async def fake_classify(subject, body, sender):
        reclass_called["n"] += 1
        return "demande_client"

    async def fake_generate(**kwargs):
        return SimpleNamespace(
            draft="BROUILLON DETERMINISTE REFUS ILLEGAL",
            raw_draft="x", language="fr", rag_pairs=[], model_used="fake",
            category="demande_client", vault_notes=[], suggested_subject=None,
        )

    monkeypatch.setattr("app.web.api.classify", fake_classify)
    monkeypatch.setattr("app.web.api.generate_draft", fake_generate)

    resp = client.post("/api/drafts/614/retry")

    assert resp.status_code == 200
    assert reclass_called["n"] == 1  # classify a bien été appelé
    # Le brouillon déterministe est retourné (pas l'ancien LLM).
    assert "BROUILLON DETERMINISTE REFUS ILLEGAL" in resp.text
    assert "ANCIEN BROUILLON LLM INADAPTE" not in resp.text

    # DB : category reclassée en demande_client + ai_draft remplacé.
    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute(
        "SELECT category, ai_draft FROM mail_processed WHERE id=614"
    ).fetchone()
    conn.close()
    assert row[0] == "demande_client"
    assert row[1] == "BROUILLON DETERMINISTE REFUS ILLEGAL"


def test_retry_no_draft_when_category_stays_non_demande(client, monkeypatch):
    """Si classify laisse le mail hors draft_categories (vrai spam), pas de brouillon."""
    async def fake_classify(subject, body, sender):
        return "spam"

    generate_called = {"n": 0}

    async def fake_generate(**kwargs):
        generate_called["n"] += 1
        return SimpleNamespace(draft="NE DEVRAIT PAS ETRE APPELE", raw_draft="x",
                               language="fr", rag_pairs=[], model_used="fake",
                               category="spam", vault_notes=[], suggested_subject=None)

    monkeypatch.setattr("app.web.api.classify", fake_classify)
    monkeypatch.setattr("app.web.api.generate_draft", fake_generate)

    resp = client.post("/api/drafts/614/retry")

    assert resp.status_code == 200
    assert generate_called["n"] == 0  # generate_draft n'est jamais appelé
    assert "Mail classé" in resp.text
    assert "spam" in resp.text
    # L'ancien brouillon LLM est conservé (pas écrasé).
    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute(
        "SELECT category, ai_draft FROM mail_processed WHERE id=614"
    ).fetchone()
    conn.close()
    assert row[0] == "spam"
    assert row[1] == "ANCIEN BROUILLON LLM INADAPTE"
