"""Tests des fixes v1.25.22 — garde-fous anti-crash silencieux (#629).

Couvre :
- Reply-To prioritaire comme email client (forwarders WP). Bug B.
- Regex « nom » stricte : ne matche plus « ce nom » au milieu d'une phrase. Bug D.
- Sujet de brouillon lisible quand le sujet original est un template WP. Bug E.
- Réconcilieur Drafts : recherche du brouillon par header X-Detective-Mail-Id.
- Réconcilieur : propagation du reply_to depuis la DB vers l'IncomingMail.

Cas de référence : mail #629 (Christèle Kremp-Voinova, Reply-To ckremp@vo.lu,
forwarder mail@detectivebelgique.be, body pollué par le chrome marketing
wikipreneurs.be).
"""

from __future__ import annotations

import pytest

from app.delivery.imap_draft import _build_draft_body
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult
from app.pipeline.qualification_builder import (
    _extract_client_info,
    suggested_subject_for_draft,
)
from app.pipeline.subject_fixer import mask_forwarder_sender

# --- Bug B : Reply-To prioritaire comme email client -------------------------


def test_mask_forwarder_sender_uses_reply_to() -> None:
    """#629 — un forwarder WP avec Reply-To = vrai email client."""
    displayed = mask_forwarder_sender(
        "mail@detectivebelgique.be",
        body="Nom: KREMP-VOINOVA",
        reply_to="ckremp@vo.lu",
    )
    assert displayed == "ckremp@vo.lu"


def test_mask_forwarder_sender_no_reply_to_forwarder() -> None:
    """Forwarder WP sans Reply-To ni email dans le body -> NO_EMAIL_IN_THE_FORM."""
    displayed = mask_forwarder_sender(
        "wordpress@detectivebelgium.com", body="Bonjour", reply_to=""
    )
    assert displayed == "NO_EMAIL_IN_THE_FORM"


def test_mask_forwarder_sender_ignores_internal_reply_to() -> None:
    """Un Reply-To interne (no-reply / domaine Detective) n'est pas un client."""
    displayed = mask_forwarder_sender(
        "mail@detectivebelgique.be", body="x", reply_to="no-reply@detectivebelgique.be"
    )
    # reply_to interne rejeté -> on retombe sur le forwarder sans email body.
    assert displayed == "NO_EMAIL_IN_THE_FORM"


def test_extract_client_info_reply_to_prioritaire() -> None:
    """Le Reply-To écrase tout email glané dans le body (cas du scammeur)."""
    body = "Nom: KREMP-VOINOVA\nContact: scammeur@poisson.com"
    info = _extract_client_info(body, "mail@detectivebelgique.be", reply_to="ckremp@vo.lu")
    assert info["email"] == "ckremp@vo.lu"


def test_extract_client_info_sans_reply_to_body_gagne() -> None:
    """Sans Reply-To, l'email du body est conservé."""
    body = "Nom: DUPONT\nMon email: client@example.com"
    info = _extract_client_info(body, "x@y.com", reply_to="")
    assert info["email"] == "client@example.com"


# --- Bug D : regex « nom » stricte -------------------------------------------


def test_extract_nom_label_debut_ligne() -> None:
    """« Nom: » en début de ligne de formulaire WP est capturé."""
    body = "Nom: KREMP-VOINOVA\nPrénom: CHRISTELE"
    info = _extract_client_info(body, "mail@detectivebelgique.be")
    assert info["nom"] == "KREMP-VOINOVA"


def test_extract_nom_ne_matche_pas_ce_nom_au_milieu() -> None:
    """« ce nom » au milieu d'une phrase NE doit PAS être capturé (régression #629)."""
    body = (
        "Bonjour, je consulte votre site car je veux connaitre ce nom de domaine "
        "et aussi ce nom de société pour mon voisin."
    )
    info = _extract_client_info(body, "x@y.com")
    assert info.get("nom") is None


def test_extract_nom_mon_nom_est_naturel() -> None:
    """« mon nom est » (formulation naturelle) reste capturé."""
    body = "Bonjour, mon nom est Bassem Sophie."
    info = _extract_client_info(body, "x@y.com")
    assert info["nom_complet"] is not None
    assert "Bassem" in info["nom_complet"]


# --- Bug E : sujet de brouillon lisible --------------------------------------


def test_suggested_subject_template_wp_avec_nom() -> None:
    """Sujet template WP + body avec Nom/Prénom -> sujet lisible représentatif."""
    body = "Nom: KREMP-VOINOVA\nPrénom: CHRISTELE\nVotre profil ?: Particulier"
    subj = suggested_subject_for_draft(
        "Nouveau Message De Détective privé Belgique - Prenons contact",
        body,
        "mail@detectivebelgique.be",
        "non_determine",
    )
    assert subj is not None
    assert "Kremp" in subj  # nom du client présent
    assert "Demande" in subj  # libellé du cas


def test_suggested_subject_sujet_normal_renvoie_none() -> None:
    """Un sujet déjà pertinent (non template WP) -> None (on garde l'original)."""
    subj = suggested_subject_for_draft(
        "Demande de filature - suspicion infidélité",
        "Nom: Dupont",
        "client@example.com",
        "infidelite_filature",
    )
    assert subj is None


# --- Bug C : réconcilieur Drafts (recherche par header marker) ---------------


class _FakeSearchResult:
    def __init__(self, ok: bool, lines: list[bytes]) -> None:
        self.result = "OK" if ok else "NO"
        self.lines = lines


class _FakeImapClient:
    """Faux client IMAP pour tester _draft_present sans vraie connexion."""

    def __init__(self, header_lines: list[bytes], body_lines: list[bytes]) -> None:
        self._header_lines = header_lines
        self._body_lines = body_lines
        self.search_calls: list[str] = []

    async def select(self, folder: str) -> _FakeSearchResult:
        return _FakeSearchResult(ok=True, lines=[])

    async def search(self, criteria: str) -> _FakeSearchResult:
        self.search_calls.append(criteria)
        if "HEADER X-Detective-Mail-Id" in criteria:
            return _FakeSearchResult(ok=True, lines=self._header_lines)
        if 'BODY "EMAIL #' in criteria:
            return _FakeSearchResult(ok=True, lines=self._body_lines)
        return _FakeSearchResult(ok=True, lines=[])


@pytest.mark.asyncio
async def test_draft_present_par_header_marker() -> None:
    """Le brouillon est trouvé via le header X-Detective-Mail-Id (v1.25.22+)."""
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[b"1"], body_lines=[])
    found = await _draft_present(client, "Drafts", 629)
    assert found is True
    assert any("HEADER X-Detective-Mail-Id 629" in c for c in client.search_calls)


@pytest.mark.asyncio
async def test_draft_absent_retourne_false() -> None:
    """Aucun match header ni body -> False (brouillon manquant, à re-livrer)."""
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[], body_lines=[])
    found = await _draft_present(client, "Drafts", 999)
    assert found is False


@pytest.mark.asyncio
async def test_draft_present_fallback_body_legacy() -> None:
    """Brouillon legacy (sans header marker) retrouvé via le body 'EMAIL #<id>'."""
    from app.workers.drafts_reconciler import _draft_present

    client = _FakeImapClient(header_lines=[], body_lines=[b"42"])
    found = await _draft_present(client, "Drafts", 42)
    assert found is True


def test_rebuild_inputs_propage_reply_to() -> None:
    """_rebuild_inputs injecte reply_to depuis la DB dans l'IncomingMail."""
    from app.config import get_settings
    from app.workers.drafts_reconciler import _rebuild_inputs

    settings = get_settings()
    mailbox = settings.mailboxes()[0]
    mail = {
        "id": 629,
        "imap_uid": "UID-629",
        "mailbox_name": mailbox.name,
        "subject": "Nouveau Message",
        "sender": "mail@detectivebelgique.be",
        "received_at": "Mon, 23 Jun 2026 12:00:00 +0000",
        "ai_draft": "Bonjour, proposition...",
        "body": "Nom: KREMP-VOINOVA",
        "delivered_at": None,
        "reply_to": "ckremp@vo.lu",
    }
    incoming, gen = _rebuild_inputs(mail, mailbox)
    assert incoming.reply_to == "ckremp@vo.lu"
    assert gen.draft == "Bonjour, proposition..."


# --- _build_draft_body : bandeau affiche le reply_to -------------------------


def test_build_draft_body_affiche_reply_to() -> None:
    """Le bandeau du brouillon affiche le Reply-To (vrai client), pas le forwarder."""
    incoming = IncomingMail(
        sender="mail@detectivebelgique.be",
        subject="Nouveau Message",
        body="Nom: KREMP-VOINOVA",
        received_at="Mon, 23 Jun 2026",
        message_id="UID-629",
        reply_to="ckremp@vo.lu",
    )
    gen = GenerationResult(
        draft="Bonjour Christèle, proposition...",
        raw_draft="Bonjour Christèle, proposition...",
        language="fr",
        rag_pairs=[],
        model_used="test",
        category="demande_client",
    )
    body_text = _build_draft_body(incoming, gen, mail_id=629, base_url="https://detective.digitalhs.biz")
    assert "EMAIL #629" in body_text
    assert "ckremp@vo.lu" in body_text  # reply_to affiché, pas le forwarder
    assert "X-Detective-Mail-Id" not in body_text  # marker header, pas dans le body texte
