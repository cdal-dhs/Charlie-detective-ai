"""Tests du shortcut citation Daniel dans _is_web_followup (app/web/api.py).

v1.25.7 - Cf. #606 (Van Houtte) : la régénération cockpit doit produire le
brouillon ack (accusé de réception) quand le mail cite un mail de Daniel, même
sans mail initial du sender en DB (traité hors-agent / autre boîte). La citation
(préfixe > + signature cabinet) est la preuve d'un échange.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from app.web.api import _is_web_followup

_SCHEMA = """
CREATE TABLE mail_processed (
    id INTEGER PRIMARY KEY,
    sender TEXT,
    received_at TEXT,
    category TEXT
)
"""

_BODY_606 = (
    "Bonjour Monsieur\n\nJe vous ai répondu en vert sur votre mail\n\n"
    "Bien à vous\n\nFrédéric Van Houtte\n\n"
    "> Le 16 juin 2026 à 15:23, Detective Belgique <contact@detectivebelgique.be> a écrit :\n"
    ">\n> MISSION : OUVRIER EN INCAPACITÉ DE TRAVAIL\n"
    "> Daniel Hurchon - DetectiveBelgique.be SRL - GSM - 0471/31.81.20"
)


@pytest.mark.asyncio
async def test_web_followup_true_when_quotes_daniel_no_history(tmp_path: Path) -> None:
    """#606 - citation Daniel + DB vide (pas d'historique sender) → follow-up."""
    db_path = tmp_path / "agent_state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
        is_fup = await _is_web_followup(
            db, "etsvanhoutte@gmail.com", "Re: Mission ouvrier en maladie", _BODY_606
        )
    assert is_fup is True


@pytest.mark.asyncio
async def test_web_followup_false_when_quote_without_daniel_signature(
    tmp_path: Path,
) -> None:
    """Citation (>) sans signature cabinet + DB vide → pas de shortcut → False."""
    body = (
        "Bonjour,\n\nMerci.\n\n"
        "> Le 16 juin, Toto <toto@example.com> a écrit :\n> Voici un mail quelconque"
    )
    db_path = tmp_path / "agent_state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
        is_fup = await _is_web_followup(db, "client@example.com", "Re: Demande", body)
    assert is_fup is False


@pytest.mark.asyncio
async def test_web_followup_false_when_no_reply_markers(tmp_path: Path) -> None:
    """Nouvelle demande (pas Re:, pas citation) + DB vide → False."""
    db_path = tmp_path / "agent_state.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
        is_fup = await _is_web_followup(
            db, "client@example.com", "Demande de devis", "Bonjour, je veux un devis."
        )
    assert is_fup is False
