from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cerveau_dossier import derive_dossier_id, extract_dossier_ref
from app.workers.imap_poller import _process_single_mail


class FakeMailbox:
    name = "detective_belgique"
    user = "contact@detectivebelgique.be"
    default_lang = "fr"
    brand = "Detective Belgique"
    imap_host = "mail.infomaniak.com"
    imap_port = 993
    short_code = "D_FR"
    cerveau2_marque = "detectivebelgique"


# --- Tests dossier_id ---


def test_extract_dossier_ref_from_subject():
    assert extract_dossier_ref("Dossier ADF - suivi") == "ADF"
    assert extract_dossier_ref("Affaire: XYZ123") == "XYZ123"
    assert extract_dossier_ref("FW: RE: ADF") == "ADF"
    assert extract_dossier_ref("Bonjour") is None


def test_derive_dossier_id_from_ref():
    did = derive_dossier_id(
        sender="foo@bar.com",
        subject="Dossier ADF",
        marque="detective_belgique",
    )
    assert did == "ADF"


def test_derive_dossier_id_from_anonymized():
    did = derive_dossier_id(
        sender="foo@bar.com",
        subject="Bonjour",
        anonymized_name="D*****e",
        marque="detective_belgique",
    )
    assert did == "detective_belgique_de"


def test_derive_dossier_id_from_sender():
    did = derive_dossier_id(
        sender="jean.dupont@email.com",
        subject="Demande",
        marque="detective_belgique",
    )
    assert did == "detective_belgique_jeandupont"


# --- Tests hook poller ---


@pytest.fixture
def mock_settings(monkeypatch):
    s = MagicMock()
    s.dry_run = False
    s.cerveau2_base_url = "https://cerveau2-det.digitalhs.biz"
    s.cerveau2_api_secret = "test-secret"
    s.db_agent_state = MagicMock()
    s.poll_interval_seconds = 300
    s.imap_host = "mail.infomaniak.com"
    s.imap_port = 993
    s.process_since_date = ""
    monkeypatch.setattr("app.workers.imap_poller.get_settings", lambda: s)
    return s


@pytest.fixture
def mock_msg():
    m = MagicMock()
    m.get.side_effect = lambda k, default="": {
        "From": "client@example.com",
        "Subject": "Dossier ADF",
        "Date": "Mon, 15 Jun 2026 10:30:00 +0200",
        "Message-ID": "<abc123@example.com>",
    }.get(k, default)
    m.is_multipart.return_value = False
    m.get_payload.return_value = b"Bonjour, voici ma demande."
    m.get_content_type.return_value = "text/plain"
    return m


def _make_mock_client(mock_msg):
    fetch_resp = MagicMock()
    fetch_resp.result = "OK"
    fetch_resp.lines = [b"", b"raw email bytes"]

    store_resp = MagicMock()
    store_resp.result = "OK"

    client = AsyncMock()
    client.fetch = AsyncMock(return_value=fetch_resp)
    client.store = AsyncMock(return_value=store_resp)
    return client


@pytest.mark.asyncio
async def test_poller_feeds_cerveau2_for_draft(mock_settings, mock_msg):
    """Un mail demande_client doit alimenter Cerveau2."""
    client = _make_mock_client(mock_msg)
    mailbox = FakeMailbox()

    with (
        patch("app.workers.imap_poller.get_settings", return_value=mock_settings),
        patch("app.workers.imap_poller.message_from_bytes", return_value=mock_msg),
        patch("app.workers.imap_poller._get_body_text", return_value="Bonjour"),
        patch("app.workers.imap_poller._mail_exists", return_value=False),
        patch("app.workers.imap_poller._persist", return_value=42),
        patch("app.workers.imap_poller.classify", return_value="demande_client"),
        patch("app.workers.imap_poller.assign_priority", return_value="high"),
        patch("app.workers.imap_poller.quick_classify", return_value=None),
        patch("app.workers.imap_poller.detect_language", return_value="fr"),
        patch("app.workers.imap_poller.is_logical_duplicate", return_value=(False, None)),
        patch("app.workers.imap_poller._refresh_thread_subject", return_value=None),
        patch("app.workers.imap_poller.generate_draft", new_callable=AsyncMock) as mock_gen,
        patch("app.workers.imap_poller.feed_correspondance", new_callable=AsyncMock) as mock_feed,
    ):
        mock_gen.return_value = MagicMock(draft="Brouillon test")
        await _process_single_mail(client, "12345", mailbox)

        mock_feed.assert_called_once()
        call_kwargs = mock_feed.call_args.kwargs
        assert call_kwargs["direction"] == "in"
        assert call_kwargs["dossier_id"] == "ADF"
        assert call_kwargs["categorie"] == "demande_client"


@pytest.mark.asyncio
async def test_poller_skips_newsletter(mock_settings, mock_msg):
    """Un mail newsletter ne doit PAS alimenter Cerveau2."""
    client = _make_mock_client(mock_msg)
    mailbox = FakeMailbox()

    with (
        patch("app.workers.imap_poller.get_settings", return_value=mock_settings),
        patch("app.workers.imap_poller.message_from_bytes", return_value=mock_msg),
        patch("app.workers.imap_poller._get_body_text", return_value="Newsletter"),
        patch("app.workers.imap_poller._mail_exists", return_value=False),
        patch("app.workers.imap_poller._persist", return_value=42),
        patch("app.workers.imap_poller.classify", return_value="newsletter"),
        patch("app.workers.imap_poller.assign_priority", return_value="low"),
        patch("app.workers.imap_poller.quick_classify", return_value=None),
        patch("app.workers.imap_poller.feed_correspondance", new_callable=AsyncMock) as mock_feed,
    ):
        await _process_single_mail(client, "12345", mailbox)

        mock_feed.assert_not_called()


@pytest.mark.asyncio
async def test_poller_skips_phishing(mock_settings, mock_msg):
    """Un mail phishing ne doit PAS alimenter Cerveau2."""
    client = _make_mock_client(mock_msg)
    mailbox = FakeMailbox()

    with (
        patch("app.workers.imap_poller.get_settings", return_value=mock_settings),
        patch("app.workers.imap_poller.message_from_bytes", return_value=mock_msg),
        patch("app.workers.imap_poller._get_body_text", return_value="Click here"),
        patch("app.workers.imap_poller._mail_exists", return_value=False),
        patch("app.workers.imap_poller._persist", return_value=42),
        patch("app.workers.imap_poller.classify", return_value="phishing"),
        patch("app.workers.imap_poller.assign_priority", return_value="high"),
        patch("app.workers.imap_poller.quick_classify", return_value=None),
        patch("app.workers.imap_poller.feed_correspondance", new_callable=AsyncMock) as mock_feed,
    ):
        await _process_single_mail(client, "12345", mailbox)

        mock_feed.assert_not_called()


@pytest.mark.asyncio
async def test_poller_feeds_other_categories(mock_settings, mock_msg):
    """Un mail 'facture' ou 'rappel' doit alimenter Cerveau2."""
    client = _make_mock_client(mock_msg)
    mailbox = FakeMailbox()

    with (
        patch("app.workers.imap_poller.get_settings", return_value=mock_settings),
        patch("app.workers.imap_poller.message_from_bytes", return_value=mock_msg),
        patch("app.workers.imap_poller._get_body_text", return_value="Facture impayée"),
        patch("app.workers.imap_poller._mail_exists", return_value=False),
        patch("app.workers.imap_poller._persist", return_value=42),
        patch("app.workers.imap_poller.classify", return_value="facture"),
        patch("app.workers.imap_poller.assign_priority", return_value="normal"),
        patch("app.workers.imap_poller.quick_classify", return_value=None),
        patch("app.workers.imap_poller.is_logical_duplicate", return_value=(False, None)),
        patch("app.workers.imap_poller._refresh_thread_subject", return_value=None),
        patch("app.workers.imap_poller.feed_correspondance", new_callable=AsyncMock) as mock_feed,
    ):
        await _process_single_mail(client, "12345", mailbox)

        mock_feed.assert_called_once()
        assert mock_feed.call_args.kwargs["categorie"] == "facture"
