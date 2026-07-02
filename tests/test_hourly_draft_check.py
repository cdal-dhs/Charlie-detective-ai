"""Tests TDD pour le worker hourly_draft_check v1.29.1.

Objectif : relancer la génération de brouillon pour les mails demande_client
pending qui n'ont PAS de brouillon (rattrapage auto en cas de crash LLM
transitoire ou de deadlock poller).

Teste :
- _fetch_missing_drafts : query SQL avec tous les critères obligatoires
- _generate_for_mail : max 3 retries, succès/échec
- _process_one : idempotence (si draft_generated=1 entre query et APPEND)
- _process_one : mise à jour DB après succès
- _process_one : pas d'update DB si génération échoue
- run_hourly_check : ne fait rien si 0 candidat
- _scope_cutoff_iso : 7 jours ISO 8601
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from app.pipeline.generator import GenerationResult
from app.workers.hourly_draft_check import (
    MAX_RETRIES_PER_MAIL,
    SCOPE_DAYS,
    _fetch_missing_drafts,
    _generate_for_mail,
    _process_one,
    _scope_cutoff_iso,
    run_hourly_check,
)


# --- Fixtures ---


@pytest.fixture
async def db_with_pending(tmp_path: Path):
    """Crée une DB mail_processed minimale avec plusieurs cas de figure."""
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE mail_processed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_name TEXT,
                subject TEXT,
                sender TEXT,
                body TEXT,
                body_preview TEXT,
                received_at TEXT,
                reply_to TEXT,
                status TEXT,
                category TEXT,
                ai_draft TEXT,
                draft_generated INTEGER DEFAULT 0,
                processed_at TEXT
            )
            """
        )
        # Cas 1 : demande_client pending SANS draft → candidat
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite1", "Demande A", "client1@test.com", "Bonjour", "pending",
             "demande_client", "", 0, "2026-06-29T10:00:00"),
        )
        # Cas 2 : demande_client pending AVEC draft → skip
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite1", "Demande B", "client2@test.com", "Bonjour", "pending",
             "demande_client", "Brouillon déjà généré", 1, "2026-06-29T10:00:00"),
        )
        # Cas 3 : demande_client approved → skip
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite1", "Demande C", "client3@test.com", "Bonjour", "approved",
             "demande_client", "", 0, "2026-06-29T10:00:00"),
        )
        # Cas 4 : newsletter pending SANS draft → skip (autre catégorie)
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite1", "Newsletter", "newsletter@test.com", "Promo", "pending",
             "newsletter", "", 0, "2026-06-29T10:00:00"),
        )
        # Cas 5 : ancien (avant scope 7j) → skip
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite1", "Demande D", "client4@test.com", "Bonjour", "pending",
             "demande_client", "", 0, "2026-01-01T10:00:00"),
        )
        # Cas 6 : demande_client pending sans draft mais draft_generated=1 → skip
        await db.execute(
            """INSERT INTO mail_processed
               (mailbox_name, subject, sender, body, status, category,
                ai_draft, draft_generated, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("boite2", "Demande E", "client5@test.com", "Bonjour", "pending",
             "demande_client", "", 1, "2026-06-29T10:00:00"),
        )
        await db.commit()
    return db_path


def _make_mailbox(name: str = "boite1") -> MagicMock:
    """Mock minimal de MailboxConfig pour les tests."""
    mb = MagicMock()
    mb.name = name
    mb.imap_host = "imap.test.com"
    mb.imap_port = 993
    mb.user = f"{name}@test.com"
    mb.app_password = "test-pwd"
    mb.default_lang = "fr"
    return mb


# --- Tests _scope_cutoff_iso ---


def test_scope_cutoff_iso_is_7_days_ago():
    cutoff = _scope_cutoff_iso()
    parsed = datetime.fromisoformat(cutoff)
    now = datetime.now(UTC)
    delta = (now - parsed).total_seconds()
    # 7 jours ± 5 secondes de marge
    assert abs(delta - SCOPE_DAYS * 86400) < 5


# --- Tests _fetch_missing_drafts ---


async def test_fetch_missing_drafts_picks_only_pending_no_draft(db_with_pending):
    mails = await _fetch_missing_drafts(db_with_pending)
    assert len(mails) == 1
    assert mails[0]["sender"] == "client1@test.com"
    assert mails[0]["subject"] == "Demande A"
    assert mails[0]["mailbox_name"] == "boite1"


async def test_fetch_missing_drafts_excludes_duplicates(db_with_pending):
    """Une demande_client pending SANS draft doit être candidate, même si elle
    a un draft_generated=0 (le poller n'a rien fait). Le test vérifie qu'on
    ne loupe pas le cas draft='' ET draft_generated=0 (le seul candidat)."""
    mails = await _fetch_missing_drafts(db_with_pending)
    ids = {m["id"] for m in mails}
    # Cas 6 : draft_generated=1 + ai_draft='' ne doit PAS être candidat
    # (sécurité anti-doublon avec poller)
    # On l'identifie par sender=client5
    assert all(m["sender"] != "client5@test.com" for m in mails)


async def test_fetch_missing_drafts_excludes_out_of_scope(db_with_pending):
    """Un mail > 7 jours ne doit pas être candidat (perf)."""
    mails = await _fetch_missing_drafts(db_with_pending)
    # Cas 5 = janvier 2026 → exclu
    assert all(m["subject"] != "Demande D" for m in mails)


async def test_fetch_missing_drafts_excludes_non_demande_client(db_with_pending):
    """Seules les demande_client sont candidates (pas les newsletter)."""
    mails = await _fetch_missing_drafts(db_with_pending)
    assert all(m["category"] == "demande_client" for m in mails)


# --- Tests _generate_for_mail ---


async def test_generate_for_mail_success():
    """Si generate_draft retourne un draft non-vide, _generate_for_mail le retourne."""
    mail = {"subject": "S", "body": "B", "sender": "a@b.c", "reply_to": ""}
    mb = _make_mailbox()
    expected = GenerationResult(
        draft="Bonjour, voici ma réponse.",
        raw_draft="raw",
        language="fr",
        rag_pairs=[],
        model_used="test-model",
        category="demande_client",
    )
    with patch("app.workers.hourly_draft_check.generate_draft", AsyncMock(return_value=expected)):
        result = await _generate_for_mail(mail, mb)
    assert result.draft == "Bonjour, voici ma réponse."


async def test_generate_for_mail_retries_on_empty_draft():
    """Si la 1ère tentative retourne draft vide, on retente."""
    mail = {"subject": "S", "body": "B", "sender": "a@b.c", "reply_to": ""}
    mb = _make_mailbox()
    empty_result = GenerationResult(
        draft="", raw_draft="", language="fr", rag_pairs=[],
        model_used="test", category="demande_client",
    )
    valid_result = GenerationResult(
        draft="Vrai brouillon", raw_draft="raw", language="fr", rag_pairs=[],
        model_used="test", category="demande_client",
    )
    mock_gen = AsyncMock(side_effect=[empty_result, valid_result])
    with patch("app.workers.hourly_draft_check.generate_draft", mock_gen):
        result = await _generate_for_mail(mail, mb)
    assert result.draft == "Vrai brouillon"
    assert mock_gen.call_count == 2


async def test_generate_for_mail_raises_after_max_retries():
    """Si toutes les tentatives échouent (raise), on lève RuntimeError après 3 essais."""
    mail = {"subject": "S", "body": "B", "sender": "a@b.c", "reply_to": ""}
    mb = _make_mailbox()
    mock_gen = AsyncMock(side_effect=RuntimeError("LLM down"))
    with patch("app.workers.hourly_draft_check.generate_draft", mock_gen):
        with pytest.raises(RuntimeError, match="max retries exhausted"):
            await _generate_for_mail(mail, mb)
    assert mock_gen.call_count == MAX_RETRIES_PER_MAIL


# --- Tests _process_one ---


async def test_process_one_idempotent_skips_if_already_generated(db_with_pending):
    """Si draft_generated=1 entre la query et le traitement, on skip (return True)."""
    # Récupère l'ID du cas 6 (boite2) : draft_generated=1
    async with aiosqlite.connect(db_with_pending) as db:
        async with db.execute(
            "SELECT id FROM mail_processed WHERE sender='client5@test.com'"
        ) as cursor:
            row = await cursor.fetchone()
    mail_id = row[0]

    mail = {
        "id": mail_id,
        "mailbox_name": "boite2",
        "subject": "Demande E",
        "sender": "client5@test.com",
        "body": "Bonjour",
        "received_at": "2026-06-29T10:00:00",
        "reply_to": "",
        "status": "pending",
    }
    mb = _make_mailbox("boite2")
    imap = MagicMock()
    result = await _process_one(db_with_pending, mail, mb, imap)
    assert result is True  # traité par ailleurs = succès
    # Aucun appel LLM ni IMAP
    imap.append.assert_not_called()


async def test_process_one_updates_db_on_success(db_with_pending):
    """Si génération + APPEND OK, ai_draft + draft_generated=1 sont persistés."""
    async with aiosqlite.connect(db_with_pending) as db:
        async with db.execute(
            "SELECT id FROM mail_processed WHERE sender='client1@test.com'"
        ) as cursor:
            row = await cursor.fetchone()
    mail_id = row[0]

    mail = {
        "id": mail_id,
        "mailbox_name": "boite1",
        "subject": "Demande A",
        "sender": "client1@test.com",
        "body": "Bonjour",
        "received_at": "2026-06-29T10:00:00",
        "reply_to": "",
        "status": "pending",
    }
    mb = _make_mailbox("boite1")
    imap = MagicMock()
    gen_ok = GenerationResult(
        draft="Brouillon généré", raw_draft="raw", language="fr", rag_pairs=[],
        model_used="test", category="demande_client",
    )
    with patch("app.workers.hourly_draft_check.generate_draft", AsyncMock(return_value=gen_ok)), \
         patch("app.workers.hourly_draft_check.append_draft", AsyncMock(return_value=True)):
        result = await _process_one(db_with_pending, mail, mb, imap)
    assert result is True

    # Vérifier DB
    async with aiosqlite.connect(db_with_pending) as db:
        async with db.execute(
            "SELECT ai_draft, draft_generated FROM mail_processed WHERE id = ?",
            (mail_id,),
        ) as cursor:
            row = await cursor.fetchone()
    assert row[0] == "Brouillon généré"
    assert row[1] == 1


async def test_process_one_returns_false_on_generation_failure(db_with_pending):
    """Si la génération LLM échoue (3 retries KO), return False sans toucher la DB."""
    async with aiosqlite.connect(db_with_pending) as db:
        async with db.execute(
            "SELECT id FROM mail_processed WHERE sender='client1@test.com'"
        ) as cursor:
            row = await cursor.fetchone()
    mail_id = row[0]

    mail = {
        "id": mail_id,
        "mailbox_name": "boite1",
        "subject": "Demande A",
        "sender": "client1@test.com",
        "body": "Bonjour",
        "received_at": "2026-06-29T10:00:00",
        "reply_to": "",
        "status": "pending",
    }
    mb = _make_mailbox("boite1")
    imap = MagicMock()
    with patch("app.workers.hourly_draft_check.generate_draft", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await _process_one(db_with_pending, mail, mb, imap)
    assert result is False

    # DB inchangée
    async with aiosqlite.connect(db_with_pending) as db:
        async with db.execute(
            "SELECT ai_draft, draft_generated FROM mail_processed WHERE id = ?",
            (mail_id,),
        ) as cursor:
            row = await cursor.fetchone()
    assert row[0] == ""
    assert row[1] == 0


# --- Test run_hourly_check end-to-end (avec mock) ---


async def test_run_hourly_check_no_candidate(tmp_path, monkeypatch):
    """Si aucun candidat, run_hourly_check retourne immédiatement sans planter."""
    db_path = tmp_path / "empty.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE mail_processed (
                id INTEGER PRIMARY KEY,
                category TEXT, status TEXT, ai_draft TEXT,
                draft_generated INTEGER, processed_at TEXT,
                mailbox_name TEXT, subject TEXT, sender TEXT,
                body TEXT, body_preview TEXT, received_at TEXT, reply_to TEXT
            )"""
        )

    settings = MagicMock()
    settings.db_agent_state = db_path
    settings.mailboxes = MagicMock(return_value=[_make_mailbox()])
    monkeypatch.setattr("app.workers.hourly_draft_check.get_settings", lambda: settings)

    stats = await run_hourly_check()
    assert stats == {"processed": 0, "ok": 0, "failed": 0, "mailboxes": 0}
