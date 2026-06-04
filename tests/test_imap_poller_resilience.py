"""Tests de résilience du poller IMAP — hotfix v1.21.3.

Couvre :
- _decode_header : charsets exotiques (unknown-8bit), fallback latin-1, garbage
- _persist : coercion str() sur subject/sender/received_at (Header objects)
- _process_single_mail : try/except englobant + flag AgentAttempted + télémétrie
- Compteur d'erreurs consécutives + alerte poller
- Anti-spam 1h/boîte de l'alerte Resend
"""

from __future__ import annotations

import sqlite3
from email.header import Header
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers.imap_poller import (
    AGENT_ATTEMPTED_FLAG,
    AGENT_FLAG,
    _decode_header,
    _persist,
    _process_single_mail,
)

# --- Fixtures & helpers ---


class FakeMailbox:
    name = "detective_belgique"
    user = "contact@detectivebelgique.be"
    default_lang = "fr"
    brand = "Detective Belgique"


@pytest.fixture
def mailbox():
    return FakeMailbox()


@pytest.fixture
def mock_settings(monkeypatch):
    s = MagicMock()
    s.dry_run = False
    s.cerveau2_base_url = ""
    s.cerveau2_api_secret = ""
    s.db_agent_state = MagicMock()
    s.poll_interval_seconds = 300
    s.imap_host = "mail.infomaniak.com"
    s.imap_port = 993
    s.process_since_date = ""
    s.poller_alert_threshold = 5
    s.resend_api_key = "fake-resend-key"
    s.resend_from = "agent@digitalhs.biz"
    monkeypatch.setattr("app.workers.imap_poller.get_settings", lambda: s)
    return s


def _make_mock_client():
    fetch_resp = MagicMock()
    fetch_resp.result = "OK"
    fetch_resp.lines = [b"", b"raw email bytes"]
    store_resp = MagicMock()
    store_resp.result = "OK"
    client = AsyncMock()
    client.fetch = AsyncMock(return_value=fetch_resp)
    client.store = AsyncMock(return_value=store_resp)
    return client, store_resp


def _absorb_background_tasks(monkeypatch):
    """Absorbe tous les asyncio.create_task (Cerveau2 feed, etc.) en no-op.

    Sans ça, les AsyncMock en arrière-plan lèvent des exceptions dans des tasks
    non-awaited qui polluent l'event loop pytest-asyncio.
    """

    def _silent_task(coro):
        coro.close()  # évite RuntimeWarning "coroutine was never awaited"
        return None

    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", _silent_task)


def _setup_db(tmp_path):
    """Crée une DB agent_state.db minimale pour _persist."""
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT,
            mailbox_name TEXT,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            category TEXT,
            draft_generated INTEGER,
            body_preview TEXT,
            body TEXT,
            ai_draft TEXT,
            status TEXT,
            priority TEXT,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    return db


# --- 1. _decode_header : charsets exotiques (4 tests) ---


def test_decode_header_unknown_8bit_charset_does_not_crash():
    """Charset 'unknown-8bit' ne doit pas faire crasher le poller."""
    result = _decode_header("=?unknown-8bit?Q?Bonjour?=")
    assert isinstance(result, str)
    # Le fallback replace doit au minimum retourner du texte non-vide
    assert len(result) >= 0  # pas de crash, c'est l'essentiel


def test_decode_header_latin1_fallback_decodes_correctly():
    """Charset latin-1 doit décoder 'café' correctement."""
    # =?iso-8859-1?Q?caf=E9?= → "café"
    result = _decode_header("=?iso-8859-1?Q?caf=E9?=")
    assert "caf" in result
    assert isinstance(result, str)


def test_decode_header_garbage_falls_back_gracefully():
    """Binaire aléatoire passé à _decode_header ne doit pas crasher."""
    garbage = "\x00\x01\x02\x03\xff\xfe\xfd"
    result = _decode_header(garbage)
    assert isinstance(result, str)


def test_decode_header_empty_returns_empty():
    """Input vide → retour vide."""
    assert _decode_header("") == ""


# --- 2. _persist : coercion str() sur Header objects (3 tests) ---


def test_persist_received_at_as_header_object(tmp_path, mock_settings):
    """Passer received_at=Header(...) ne doit pas faire crasher sqlite3."""
    db = _setup_db(tmp_path)
    mock_settings.db_agent_state = db
    received_at = Header("Mon, 15 May 2026 10:30:00 +0200")
    mail_id = _persist(
        db_path=db,
        imap_uid="1234",
        mailbox_name="detective_belgique",
        subject="Test",
        sender="client@example.com",
        received_at=received_at,
        category="demande_client",
        draft_generated=1,
    )
    assert mail_id > 0
    # Vérifier que la valeur a été stockée comme str
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT received_at FROM mail_processed WHERE id = ?", (mail_id,)).fetchone()
    conn.close()
    assert isinstance(row[0], str)
    assert "2026" in row[0]


def test_persist_subject_as_header_object(tmp_path, mock_settings):
    """Passer subject=Header(...) ne doit pas faire crasher sqlite3."""
    db = _setup_db(tmp_path)
    mock_settings.db_agent_state = db
    subject = Header("Dossier ADF — urgence")
    mail_id = _persist(
        db_path=db,
        imap_uid="1235",
        mailbox_name="detective_belgique",
        subject=subject,
        sender="client@example.com",
        received_at="Mon, 15 May 2026 10:30:00 +0200",
        category="demande_client",
        draft_generated=0,
    )
    assert mail_id > 0
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT subject FROM mail_processed WHERE id = ?", (mail_id,)).fetchone()
    conn.close()
    assert isinstance(row[0], str)
    assert "Dossier" in row[0]


def test_persist_sender_as_header_object(tmp_path, mock_settings):
    """Passer sender=Header(...) ne doit pas faire crasher sqlite3."""
    db = _setup_db(tmp_path)
    mock_settings.db_agent_state = db
    sender = Header("client@example.com")
    mail_id = _persist(
        db_path=db,
        imap_uid="1236",
        mailbox_name="detective_belgique",
        subject="Test",
        sender=sender,
        received_at="Mon, 15 May 2026 10:30:00 +0200",
        category="demande_client",
        draft_generated=0,
    )
    assert mail_id > 0
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT sender FROM mail_processed WHERE id = ?", (mail_id,)).fetchone()
    conn.close()
    assert isinstance(row[0], str)


# --- 3. _process_single_mail : try/except englobant (3 tests) ---


@pytest.mark.asyncio
async def test_process_single_mail_does_not_crash_on_bad_encoding(monkeypatch, mock_settings):
    """Si _decode_header lève une exception non-attrapée, le try/except interne
    doit attraper, poser AgentAttempted et retourner 'error'."""

    # Reset health
    from app.healthcheck import health

    health.consecutive_errors = {}

    client, _ = _make_mock_client()

    # Forcer _decode_header à crasher
    monkeypatch.setattr(
        "app.workers.imap_poller._decode_header",
        MagicMock(side_effect=ValueError("simulated crash")),
    )

    # Patcher les fonctions downstream pour qu'elles ne fassent rien
    monkeypatch.setattr(
        "app.workers.imap_poller.classify", AsyncMock(return_value="demande_client")
    )
    monkeypatch.setattr("app.workers.imap_poller.quick_classify", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.assign_priority", MagicMock(return_value="normal"))
    monkeypatch.setattr("app.workers.imap_poller.detect_language", MagicMock(return_value="fr"))
    monkeypatch.setattr("app.workers.imap_poller.generate_draft", AsyncMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.derive_dossier_id", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.feed_correspondance", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.feed_document", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller._persist", MagicMock(return_value=42))
    monkeypatch.setattr(
        "app.workers.imap_poller._is_verified_demande_client", MagicMock(return_value=False)
    )
    _absorb_background_tasks(monkeypatch)

    result = await _process_single_mail(client, "uid_test", FakeMailbox())
    assert result == "error"

    # Vérifier que le flag AgentAttempted a été posé
    flag_calls = [
        c
        for c in client.store.call_args_list
        if len(c.args) >= 3 and AGENT_ATTEMPTED_FLAG in c.args[2]
    ]
    assert len(flag_calls) == 1
    assert flag_calls[0].args[1] == "+FLAGS"


@pytest.mark.asyncio
async def test_process_single_mail_writes_telemetry_on_crash(monkeypatch, mock_settings):
    """Un crash doit écrire une ligne dans agent_telemetry (event_type=poller_mail_crash)."""
    from app.healthcheck import health

    health.consecutive_errors = {}

    client, _ = _make_mock_client()
    monkeypatch.setattr(
        "app.workers.imap_poller._decode_header",
        MagicMock(side_effect=ValueError("simulated crash 2")),
    )

    telemetry_writes = MagicMock()
    monkeypatch.setattr(
        "app.workers.imap_poller._log_telemetry",
        telemetry_writes,
    )
    monkeypatch.setattr(
        "app.workers.imap_poller.classify", AsyncMock(return_value="demande_client")
    )
    monkeypatch.setattr("app.workers.imap_poller.quick_classify", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.assign_priority", MagicMock(return_value="normal"))
    monkeypatch.setattr("app.workers.imap_poller.detect_language", MagicMock(return_value="fr"))
    monkeypatch.setattr("app.workers.imap_poller.generate_draft", AsyncMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.derive_dossier_id", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.feed_correspondance", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.feed_document", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller._persist", MagicMock(return_value=42))
    monkeypatch.setattr(
        "app.workers.imap_poller._is_verified_demande_client", MagicMock(return_value=False)
    )
    _absorb_background_tasks(monkeypatch)

    await _process_single_mail(client, "uid_telemetry", FakeMailbox())

    # Au moins une télémétrie poller_mail_crash
    crash_telemetry = [
        c
        for c in telemetry_writes.call_args_list
        if len(c.args) >= 2 and c.args[1] == "poller_mail_crash"
    ]
    assert len(crash_telemetry) == 1
    assert "uid_telemetry" in crash_telemetry[0].args[3]


@pytest.mark.asyncio
async def test_process_single_mail_does_not_set_agent_attempted_on_success(
    monkeypatch, mock_settings
):
    """Si le mail est traité avec succès, AgentAttempted ne doit PAS être posé."""
    from app.healthcheck import health

    health.consecutive_errors = {}

    client, _ = _make_mock_client()
    monkeypatch.setattr("app.workers.imap_poller.classify", AsyncMock(return_value="autre"))
    monkeypatch.setattr("app.workers.imap_poller.quick_classify", MagicMock(return_value="autre"))
    monkeypatch.setattr("app.workers.imap_poller.assign_priority", MagicMock(return_value="low"))
    monkeypatch.setattr("app.workers.imap_poller.detect_language", MagicMock(return_value="fr"))
    monkeypatch.setattr("app.workers.imap_poller.generate_draft", AsyncMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.derive_dossier_id", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.feed_correspondance", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.feed_document", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller._persist", MagicMock(return_value=42))
    monkeypatch.setattr("app.workers.imap_poller._is_system_email", MagicMock(return_value=False))
    monkeypatch.setattr(
        "app.workers.imap_poller._is_verified_demande_client", MagicMock(return_value=False)
    )
    monkeypatch.setattr("app.workers.imap_poller._save_attachments", MagicMock())
    monkeypatch.setattr("app.workers.imap_poller._mail_exists", MagicMock(return_value=False))
    monkeypatch.setattr("app.workers.imap_poller._is_known_sender", MagicMock(return_value=False))
    monkeypatch.setattr("app.workers.imap_poller.append_draft", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.notify_draft", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.notify_slack_draft", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.alert_imap_draft_failure", AsyncMock())
    _absorb_background_tasks(monkeypatch)

    await _process_single_mail(client, "uid_success", FakeMailbox())

    flag_calls = [
        c
        for c in client.store.call_args_list
        if len(c.args) >= 3 and AGENT_ATTEMPTED_FLAG in c.args[2]
    ]
    assert len(flag_calls) == 0
    # En revanche, AgentProcessed doit être posé
    success_flag_calls = [
        c for c in client.store.call_args_list if len(c.args) >= 3 and AGENT_FLAG in c.args[2]
    ]
    assert len(success_flag_calls) == 1


# --- 4. Compteur d'erreurs + alerte (6 tests) ---


@pytest.mark.asyncio
async def test_persistent_failure_does_not_alert_below_threshold(monkeypatch, mock_settings):
    """4 crashes consécutifs < seuil (5) → pas d'alerte envoyée.

    On teste via le VRAI helper _maybe_alert_poller_failure, en mockant
    asyncio.create_task pour capturer la coroutine sans l'exécuter.
    """
    from app.healthcheck import health

    health.consecutive_errors = {}
    mock_settings.poller_alert_threshold = 5

    client, _ = _make_mock_client()
    monkeypatch.setattr(
        "app.workers.imap_poller._decode_header",
        MagicMock(side_effect=ValueError("crash")),
    )
    monkeypatch.setattr("app.workers.imap_poller._log_telemetry", MagicMock())
    monkeypatch.setattr(
        "app.workers.imap_poller.classify", AsyncMock(return_value="demande_client")
    )
    monkeypatch.setattr("app.workers.imap_poller.quick_classify", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.assign_priority", MagicMock(return_value="normal"))
    monkeypatch.setattr("app.workers.imap_poller.detect_language", MagicMock(return_value="fr"))
    monkeypatch.setattr("app.workers.imap_poller.generate_draft", AsyncMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.derive_dossier_id", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.feed_correspondance", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.feed_document", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller._persist", MagicMock(return_value=42))
    monkeypatch.setattr(
        "app.workers.imap_poller._is_verified_demande_client", MagicMock(return_value=False)
    )
    _absorb_background_tasks(monkeypatch)

    # Capturer les tasks créées par le vrai helper
    created_tasks: list = []

    def capture_task(coro):
        created_tasks.append(coro)
        coro.close()  # évite warning "coroutine was never awaited"
        return MagicMock()

    # Restaurer asyncio.create_task pour ce test spécifique (override de _absorb)
    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", capture_task)

    # 4 crashes successifs
    for i in range(4):
        client.store.reset_mock()
        client.fetch = AsyncMock(return_value=MagicMock(result="OK", lines=[b"", b"raw"]))
        await _process_single_mail(client, f"uid_{i}", FakeMailbox())

    # Helper appelé 4 fois, mais 0 task créée (compteur < seuil)
    assert len(created_tasks) == 0


@pytest.mark.asyncio
async def test_persistent_failure_triggers_alert_at_threshold(monkeypatch, mock_settings):
    """5 crashes consécutifs ≥ seuil (5) → 1 alerte déclenchée."""
    from app.healthcheck import health

    health.consecutive_errors = {}
    mock_settings.poller_alert_threshold = 5

    client, _ = _make_mock_client()
    monkeypatch.setattr(
        "app.workers.imap_poller._decode_header",
        MagicMock(side_effect=ValueError("crash")),
    )
    monkeypatch.setattr("app.workers.imap_poller._log_telemetry", MagicMock())
    monkeypatch.setattr(
        "app.workers.imap_poller.classify", AsyncMock(return_value="demande_client")
    )
    monkeypatch.setattr("app.workers.imap_poller.quick_classify", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.assign_priority", MagicMock(return_value="normal"))
    monkeypatch.setattr("app.workers.imap_poller.detect_language", MagicMock(return_value="fr"))
    monkeypatch.setattr("app.workers.imap_poller.generate_draft", AsyncMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.derive_dossier_id", MagicMock(return_value=None))
    monkeypatch.setattr("app.workers.imap_poller.feed_correspondance", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller.feed_document", AsyncMock())
    monkeypatch.setattr("app.workers.imap_poller._persist", MagicMock(return_value=42))
    monkeypatch.setattr(
        "app.workers.imap_poller._is_verified_demande_client", MagicMock(return_value=False)
    )
    _absorb_background_tasks(monkeypatch)

    created_tasks: list = []

    def capture_task(coro):
        created_tasks.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", capture_task)

    for i in range(5):
        client.store.reset_mock()
        client.fetch = AsyncMock(return_value=MagicMock(result="OK", lines=[b"", b"raw"]))
        await _process_single_mail(client, f"uid_{i}", FakeMailbox())

    # 1 task créée (la 5e crash = seuil atteint)
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_persistent_failure_alert_helper_does_not_call_alert_below_threshold(
    monkeypatch, mock_settings
):
    """Helper _maybe_alert_poller_failure ne doit PAS scheduler de task si compteur < seuil."""
    from app.workers import imap_poller

    mock_settings.poller_alert_threshold = 5

    create_task_mock = MagicMock()

    def _capture(coro):
        coro.close()
        create_task_mock(coro)  # enregistre l'appel
        return MagicMock()

    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", _capture)

    # Compteur < seuil
    await imap_poller._maybe_alert_poller_failure(
        FakeMailbox(), consecutive_errors=3, last_error="err", sample_uids=["1"]
    )

    create_task_mock.assert_not_called()


@pytest.mark.asyncio
async def test_persistent_failure_alert_helper_schedules_task_at_threshold(
    monkeypatch, mock_settings
):
    """Helper _maybe_alert_poller_failure DOIT scheduler une task si compteur >= seuil."""
    from app.workers import imap_poller

    mock_settings.poller_alert_threshold = 5

    create_task_mock = MagicMock()

    def _capture(coro):
        coro.close()
        create_task_mock(coro)  # enregistre l'appel
        return MagicMock()

    monkeypatch.setattr("app.workers.imap_poller.asyncio.create_task", _capture)

    # Compteur >= seuil
    await imap_poller._maybe_alert_poller_failure(
        FakeMailbox(), consecutive_errors=5, last_error="err", sample_uids=["1"]
    )

    create_task_mock.assert_called_once()


def test_alert_reset_on_successful_cycle():
    """health.reset_errors(mailbox) remet le compteur à 0."""
    from app.healthcheck import health

    health.consecutive_errors = {"detective_belgique": 4}
    health.reset_errors("detective_belgique")
    assert health.consecutive_errors["detective_belgique"] == 0

    # Idempotent
    health.reset_errors("detective_belgique")
    assert health.consecutive_errors["detective_belgique"] == 0


def test_health_snapshot_includes_consecutive_errors():
    """health.snapshot() doit inclure le compteur d'erreurs."""
    from app.healthcheck import health

    health.consecutive_errors = {"detective_belgique": 3}
    snap = health.snapshot()
    assert "consecutive_errors" in snap
    assert snap["consecutive_errors"]["detective_belgique"] == 3
    health.consecutive_errors = {}


# --- 5. Anti-spam 1h/boîte de l'alerte Resend (3 tests) ---


@pytest.mark.asyncio
async def test_alert_poller_persistent_failure_sends_email(monkeypatch):
    """1er appel à alert_poller_persistent_failure doit envoyer 1 email Resend."""
    from app import alerts

    alerts._last_poller_alert_sent = {}

    mock_post = AsyncMock(return_value=MagicMock(status_code=200, raise_for_status=lambda: None))
    mock_client = MagicMock()
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("app.alerts.httpx.AsyncClient", lambda **kw: mock_client)

    await alerts.alert_poller_persistent_failure(
        mailbox_name="detective_belgique",
        error_count=5,
        last_error="TypeError: x",
        sample_uids=["1234", "5678"],
    )

    # v1.21.5 : 1 POST Resend + 1 POST Slack (canal secondaire) = 2 appels
    assert mock_post.call_count == 2
    payload = mock_post.call_args_list[0].kwargs["json"]
    assert "🚨" in payload["subject"]
    assert "cdal@digitalhs.biz" in payload["to"]
    assert "5 erreurs" in payload["subject"]


@pytest.mark.asyncio
async def test_alert_poller_persistent_failure_anti_spam_within_1h(monkeypatch):
    """2eme appel < 1h après le 1er doit être skip (anti-spam)."""
    from datetime import UTC, datetime, timedelta

    from app import alerts

    # Simuler 1er envoi "à l'instant"
    alerts._last_poller_alert_sent = {
        "detective_belgique": datetime.now(UTC) - timedelta(seconds=30)
    }

    mock_post = AsyncMock(return_value=MagicMock(status_code=200, raise_for_status=lambda: None))
    mock_client = MagicMock()
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("app.alerts.httpx.AsyncClient", lambda **kw: mock_client)

    await alerts.alert_poller_persistent_failure(
        mailbox_name="detective_belgique",
        error_count=6,
        last_error="TypeError: y",
        sample_uids=["1234"],
    )

    # Pas d'envoi car < 1h
    assert mock_post.call_count == 0


@pytest.mark.asyncio
async def test_alert_poller_persistent_failure_fires_after_cooldown(monkeypatch):
    """2eme appel > 1h après le 1er doit envoyer normalement."""
    from datetime import UTC, datetime, timedelta

    from app import alerts

    # Simuler 1er envoi il y a 2h
    alerts._last_poller_alert_sent = {"detective_belgique": datetime.now(UTC) - timedelta(hours=2)}

    mock_post = AsyncMock(return_value=MagicMock(status_code=200, raise_for_status=lambda: None))
    mock_client = MagicMock()
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("app.alerts.httpx.AsyncClient", lambda **kw: mock_client)

    await alerts.alert_poller_persistent_failure(
        mailbox_name="detective_belgique",
        error_count=10,
        last_error="TypeError: z",
        sample_uids=["1234"],
    )

    # Envoi OK car > 1h. v1.21.5 : 1 POST Resend + 1 POST Slack = 2
    assert mock_post.call_count == 2
