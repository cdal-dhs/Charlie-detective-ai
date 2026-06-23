"""Tests de l'endpoint cockpit POST /api/mails/{id}/fix-subject (app/web/api.py).

v1.25.3 — rétrocorrection des sujets illisibles (ex: #614 itsme cyrillique).
Vérifie : UPDATE subject + audit log original + retour HTML du nouveau sujet,
et dégradation silencieuse si le LLM ne propose rien.
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
            body TEXT
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
            "INSERT INTO mail_processed (id, subject, body) VALUES (?,?,?)",
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


@pytest.fixture
def client(tmp_path: Path, operator_user):
    db_path = _make_db(
        tmp_path,
        (614, "іtѕⅿе-Bеvеіlіngѕmеldіng", "J'aimerais prouver l'infidélité..."),
    )
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    client = TestClient(app)
    client._db_path = db_path  # type: ignore[attr-defined]
    return client


def test_fix_subject_updates_and_audits(client, monkeypatch) -> None:
    """LLM propose un sujet lisible → UPDATE + audit log original + HTML du nouveau."""

    async def fake_fix(subject, body_preview):
        return "itsme-Bevelingsmelding"

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/614/fix-subject")

    assert resp.status_code == 200
    assert "itsme-Bevelingsmelding" in resp.text
    assert "Sujet corrigé" in resp.text
    assert "іtѕⅿе" not in resp.text  # l'illisible n'est plus affiché

    # DB : subject remplacé par la version lisible.
    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=614").fetchone()
    audits = conn.execute(
        "SELECT action, details FROM audit_logs WHERE resource_id='614'"
    ).fetchall()
    conn.close()
    assert row[0] == "itsme-Bevelingsmelding"
    assert any(a[0] == "subject_fixed" for a in audits)
    # L'audit log conserve l'original (forensic).
    assert any("іtѕⅿе" in (a[1] or "") for a in audits)


def test_fix_subject_no_improvement_keeps_original(client, monkeypatch) -> None:
    """LLM ne propose rien de mieux → sujet original conservé + message d'info."""

    async def fake_fix(subject, body_preview):
        return None

    monkeypatch.setattr("app.web.api.fix_subject_llm", fake_fix)

    resp = client.post("/api/mails/614/fix-subject")

    assert resp.status_code == 200
    assert "іtѕⅿе" in resp.text  # l'original est réaffiché
    assert "Aucune correction" in resp.text

    # DB : subject inchangé.
    conn = sqlite3.connect(str(client._db_path))
    row = conn.execute("SELECT subject FROM mail_processed WHERE id=614").fetchone()
    audits = conn.execute("SELECT action FROM audit_logs WHERE resource_id='614'").fetchall()
    conn.close()
    assert row[0] == "іtѕⅿе-Bеvеіlіngѕmеldіng"
    assert any(a[0] == "subject_fix_noop" for a in audits)


def test_fix_subject_not_found_returns_404(tmp_path: Path, operator_user) -> None:
    db_path = _make_db(tmp_path, None)
    app = make_app()
    app.dependency_overrides[get_db] = _db_path_holder(db_path)
    app.dependency_overrides[require_operator] = lambda: operator_user
    client = TestClient(app)

    resp = client.post("/api/mails/9999/fix-subject")
    assert resp.status_code == 404
