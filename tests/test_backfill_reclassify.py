"""Tests du backfill de reclassification (scripts/backfill_reclassify.py).

v1.25.2 — vérifie que --only-id ignore le filtre draft_generated (pour pouvoir
retraiter un mail déjà brouillonné, ex: #614 dont le brouillon LLM inadapté doit
être remplacé par le builder déterministe illegal_refusal).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import MailboxConfig
from scripts.backfill_reclassify import (
    _fetch_candidates,
    _regenerate_draft,
)


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
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
            body TEXT,
            ai_draft TEXT,
            draft_generated INTEGER
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO mail_processed "
            "(id, imap_uid, mailbox_name, subject, sender, received_at, "
            " category, status, priority, body, ai_draft, draft_generated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            r,
        )
    conn.commit()
    conn.close()
    return db


def test_only_id_returns_mail_even_if_draft_generated(tmp_path: Path) -> None:
    """v1.25.2 — --only-id doit retourner un mail déjà brouillonné (draft_generated=1)."""
    db = _make_db(
        tmp_path,
        [
            (614, "uid1", "detective_belgique", "Demande", "x@y.com", "d",
             "phishing", "pending", "high", "body614", "ancien brouillon LLM", 1),
            (700, "uid2", "detective_belgique", "Autre", "z@y.com", "d",
             "facture", "pending", "normal", "body700", None, 0),
        ],
    )
    candidates = asyncio.run(_fetch_candidates(db, only_id=614))
    assert len(candidates) == 1
    assert candidates[0]["id"] == 614
    assert candidates[0]["category"] == "phishing"
    # Le mail 700 (facture, sans brouillon) ne doit PAS être remonté par --only-id 614.
    candidates_700 = asyncio.run(_fetch_candidates(db, only_id=700))
    assert len(candidates_700) == 1
    assert candidates_700[0]["id"] == 700


def test_only_id_not_found_returns_empty(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [])
    assert asyncio.run(_fetch_candidates(db, only_id=999)) == []


def test_bulk_scan_still_filters_undrafted(tmp_path: Path) -> None:
    """Sans --only-id, on garde le filtre draft_generated=0 (backfill bulk historique)."""
    db = _make_db(
        tmp_path,
        [
            (1, "u1", "mb", "S1", "a@x.com", "d", "facture", "p", "n", "b1", None, 0),
            (2, "u2", "mb", "S2", "b@x.com", "d", "facture", "p", "n", "b2", "draft", 1),
        ],
    )
    # Bulk (only_id=None) : ne prend que les mails SANS brouillon (draft_generated=0).
    candidates = asyncio.run(_fetch_candidates(db, categories=("facture",)))
    assert [c["id"] for c in candidates] == [1]


@pytest.fixture
def mailbox() -> MailboxConfig:
    return MailboxConfig(
        name="detective_belgique",
        user="test@detectivebelgique.be",
        app_password="x",
        brand="Detective Belgique",
        default_lang="fr",
        db_path=Path("./data/boite1.sqlite"),
    )


def test_regenerate_replaces_draft_when_reclassified_to_demande(
    mailbox: MailboxConfig, monkeypatch
) -> None:
    """--only-id sur un mail déjà brouillonné : si classify remonte en demande_client,
    le brouillon est régénéré (remplace l'ancien)."""
    mail = {
        "id": 614,
        "category": "phishing",
        "body": "J'aimerais prouver l'infidélité... combien ça coûte ? merci Serge M",
        "subject": "Demande",
        "sender": "x@y.com",
    }

    async def fake_classify(subject, body, sender):
        return "demande_client"

    async def fake_generate(**kwargs):
        return SimpleNamespace(
            draft="BROUILLON DETERMINISTE REFUS ILLEGAL",
            suggested_subject="Refus demande illégale — Serge M",
        )

    monkeypatch.setattr("scripts.backfill_reclassify.classify", fake_classify)
    monkeypatch.setattr("scripts.backfill_reclassify.generate_draft", fake_generate)

    old_cat, new_cat, draft, suggested_subject = asyncio.run(
        _regenerate_draft(mail, mailbox, apply=True)
    )

    assert old_cat == "phishing"
    assert new_cat == "demande_client"
    assert draft == "BROUILLON DETERMINISTE REFUS ILLEGAL"
    # v1.25.28 — suggested_subject propagé pour persistance + livreur backfill.
    assert suggested_subject == "Refus demande illégale — Serge M"


def test_regenerate_no_draft_when_still_not_demande(mailbox: MailboxConfig, monkeypatch) -> None:
    """Si classify laisse le mail hors demande_client (vrai spam), pas de brouillon régénéré."""
    mail = {
        "id": 999,
        "category": "spam",
        "body": "Cliquez ici pour gagner un iPhone",
        "subject": "Gagnez!",
        "sender": "spam@x.com",
    }

    async def fake_classify(subject, body, sender):
        return "spam"

    monkeypatch.setattr("scripts.backfill_reclassify.classify", fake_classify)

    old_cat, new_cat, draft, suggested_subject = asyncio.run(
        _regenerate_draft(mail, mailbox, apply=True)
    )

    assert new_cat == "spam"
    assert draft == ""
    assert suggested_subject == ""
