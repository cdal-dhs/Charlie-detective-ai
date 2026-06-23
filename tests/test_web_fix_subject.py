"""Tests de l'endpoint cockpit POST /api/mails/{id}/fix-subject (app/web/api.py).

v1.25.4 — rétrocorrection des sujets illisibles/incohérents (ex: #614 itsme
cyrillique, #515 forwarder WP « Réinitialisation du mot de passe ») + tag
[NO_EMAIL_IN_THE_FORM] pour les forwarders WP (pas d'email client).
Vérifie : UPDATE subject + audit log original + retour HTML du nouveau sujet,
et dégradation silencieuse si le LLM ne propose rien ET pas de tag à appliquer.
"""

from __future__ import annotations

# Les cyrilliques dans les fixtures sont intentionnels (test #614 homoglyphes).
# ruff: noqa: RUF001
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.web.app import make_app
from app.web.deps import get_db, require_operator


def _make_db(tmp_path: Path, mail_row: tuple | None) -> Path:
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY,
            subject TEXT,
            body TEXT,
            sender TEXT
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
    if mail_row is not None:
        conn.execute(
            "INSERT INTO mail_processed (id, subject, body, sender) VALUES (?,?,?,?)",
            mail_row,
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


def _make_client(tmp_path: Path, operator_user, mail_row: tuple | None) -> TestClient:
    db_path = _make_db(tmp_path, mail_row)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    client = TestClient(app)
    client._db_path = db_path  # type: ignore[attr-defined]
    return client


# --- #614 : homoglyphes, sender normal (pas de tag) ---


def test_fix_subject_homoglyph_no_tag(tmp_path: Path, operator_user, monkeypatch) -> None:
    """#614 — sender normal → reformulation LLM, AUCUN tag WP."""
    client = _make_client(
        tmp_path,
        operator_user,
        (
            614,
            "іtѕⅿе-Bеvеіlіngѕmеldіng",
            "J'aimerais prouver l'infidélité...",
            "yashwantsharma@colorsofindiatours.com",
        ),
    )

    async def fake_fix(subject, body_preview):
        return "itsme-Beveilingsmelding: uw dienst stopgezet"

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/614/fix-subject")

    assert resp.status_code == 200
    assert "itsme-Beveilingsmelding" in resp.text
    assert "[NO_EMAIL_IN_THE_FORM]" not in resp.text  # pas un forwarder WP

    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=614").fetchone()
    audits = conn.execute("SELECT action FROM audit_logs WHERE resource_id='614'").fetchall()
    conn.close()
    assert row[0] == "itsme-Beveilingsmelding: uw dienst stopgezet"
    assert any(a[0] == "subject_fixed" for a in audits)


# --- #515 : forwarder WP, reformulation LLM + tag ---


def test_fix_subject_wp_forwarder_rephrase_and_tag(tmp_path, operator_user, monkeypatch) -> None:
    """#515 — forwarder WP → reformulation LLM du sujet non-représentatif + tag."""
    client = _make_client(
        tmp_path,
        operator_user,
        (
            515,
            "[Privédetective België] Réinitialisation du mot de passe",
            "Hairemans Nathalie, Telefoonnummer 0468287587, infidélité...",
            "wordpress@detectivebelgium.com",
        ),
    )

    async def fake_fix(subject, body_preview):
        return "Demande de suivi — Hairemans Nathalie"

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/515/fix-subject")

    assert resp.status_code == 200
    assert "Demande de suivi — Hairemans Nathalie" in resp.text
    assert "[NO_EMAIL_IN_THE_FORM]" in resp.text  # tag ajouté

    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=515").fetchone()
    conn.close()
    assert row[0] == "Demande de suivi — Hairemans Nathalie [NO_EMAIL_IN_THE_FORM]"


def test_fix_subject_wp_forwarder_tag_only_when_llm_noop(
    tmp_path, operator_user, monkeypatch
) -> None:
    """#515 — si le LLM ne propose rien, on tag quand même le sujet original (forwarder WP)."""
    client = _make_client(
        tmp_path,
        operator_user,
        (
            515,
            "[Privédetective België] Réinitialisation du mot de passe",
            "body",
            "wordpress@detectivebelgium.com",
        ),
    )

    async def fake_fix(subject, body_preview):
        return None  # LLM ne propose rien

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/515/fix-subject")

    assert resp.status_code == 200
    assert "[NO_EMAIL_IN_THE_FORM]" in resp.text

    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=515").fetchone()
    conn.close()
    # Le sujet original est conservé + tag suffixé.
    assert row[0].endswith("[NO_EMAIL_IN_THE_FORM]")
    assert "Réinitialisation" in row[0]


# --- Dégradation silencieuse : sender normal + LLM None → noop (rien changé) ---


def test_fix_subject_noop_when_normal_sender_and_llm_none(
    tmp_path, operator_user, monkeypatch
) -> None:
    """Sender normal + LLM ne propose rien + pas de tag → sujet inchangé, message noop."""
    client = _make_client(
        tmp_path,
        operator_user,
        (700, "Demande de devis", "body", "client@gmail.com"),
    )

    async def fake_fix(subject, body_preview):
        return None

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/700/fix-subject")

    assert resp.status_code == 200
    assert "Demande de devis" in resp.text  # original réaffiché
    assert "Aucune correction" in resp.text
    assert "[NO_EMAIL_IN_THE_FORM]" not in resp.text

    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=700").fetchone()
    audits = conn.execute("SELECT action FROM audit_logs WHERE resource_id='700'").fetchall()
    conn.close()
    assert row[0] == "Demande de devis"  # inchangé
    assert any(a[0] == "subject_fix_noop" for a in audits)


def test_fix_subject_not_found_returns_404(tmp_path: Path, operator_user) -> None:
    client = _make_client(tmp_path, operator_user, None)
    resp = client.post("/api/mails/9999/fix-subject")
    assert resp.status_code == 404
