"""Tests TDD v1.28.2 — garde-fou anti-brouillon-interne (#686).

Contexte : le mail #686 (CDAL→Daniel, résumé de réunion IT) a été classé
`demande_client` et un brouillon client aberrant a été livré en IMAP Drafts.
Cause racine : aucun filtre 'sender interne' dans le préfiltre ni dans le
classifier — un mail forwardé depuis `cdal@digitalhs.biz` vers la boîte Daniel
était traité comme une demande client.

Fix (v1.28.2) :
- `is_internal_sender()` dans prefilter.py — bloque tout sender dont le
  domaine est dans `_INTERNAL_SENDER_DOMAINS` (digitalhs.biz, detective*).
- `quick_classify()` retourne `"autre"` en première position si interne.
- `_is_internal_email()` (déjà existant dans qualification_builder) est
  aussi invoqué dans `generate_draft()` pour défense en profondeur.
"""

from __future__ import annotations

from email.message import EmailMessage

from app.pipeline.prefilter import (
    is_internal_sender,
    quick_classify,
)


def _msg(from_addr: str, subject: str = "Test", body: str = "Lorem ipsum") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


# --- is_internal_sender() direct ------------------------------------------


def test_is_internal_sender_blocks_cdal_digitalhs() -> None:
    """#686 — sender CDAL@digitalhs.biz doit être reconnu interne."""
    msg = _msg("Christophe DALLA VALLE <cdal@digitalhs.biz>")
    assert is_internal_sender(msg) is True


def test_is_internal_sender_blocks_bare_address() -> None:
    """#686 variant — sender sans display name."""
    msg = _msg("cdal@digitalhs.biz")
    assert is_internal_sender(msg) is True


def test_is_internal_sender_blocks_internal_domain() -> None:
    """Tout sender sur le domaine cabinet digitalhs.biz doit être interne."""
    assert is_internal_sender(_msg("staff@digitalhs.biz")) is True
    assert is_internal_sender(_msg("ceo@digitalhs.biz")) is True


def test_is_internal_sender_blocks_named_staff_anywhere() -> None:
    """CDAL/Daniel sur n'importe quel domaine (ex : gmail.com) → interne.
    Règle d'or : faux positif acceptable sur staff identifié, plutôt que
    d'envoyer un brouillon aberrant à un humain interne."""
    assert is_internal_sender(_msg("cdal@gmail.com")) is True
    assert is_internal_sender(_msg("daniel@yahoo.fr")) is True


def test_is_internal_sender_passes_real_client() -> None:
    """Un vrai client (yahoo, gmail, hotmail…) NE doit PAS être interne."""
    assert is_internal_sender(_msg("client@example.com")) is False
    assert is_internal_sender(_msg("kirara.olivier@yahoo.fr")) is False
    assert is_internal_sender(_msg("user@gmail.com")) is False
    assert is_internal_sender(_msg("john.dupont@hotmail.fr")) is False


def test_is_internal_sender_handles_technical_forwarders() -> None:
    """Les forwarders techniques (wordpress@, no-reply@) NE sont PAS internes —
    ils ne sont pas dans la liste cabinet. Ils suivent leur propre logique
    (WP contact form → demande_client via quick_classify)."""
    assert is_internal_sender(_msg("wordpress@detectivebelgique.be")) is False
    assert is_internal_sender(_msg("noreply@external-saas.com")) is False


# --- quick_classify() — premier maillon de la défense ----------------------


def test_quick_classify_routes_internal_to_autre() -> None:
    """#686 — CDAL forward note interne → category=autre (PAS demande_client)."""
    msg = _msg(
        "Christophe DALLA VALLE <cdal@digitalhs.biz>",
        subject="Résumé du meeting 260626 // IT Detectives avec Nicolas et CDAL",
        body=(
            "Bonjour Daniel\n"
            "Voici en annexe le résumé de notre meeting avec Nicolas concernant ton IT.\n"
            "Réunion de planification sur l'automatisation administrative.\n"
            "Centralisation des boîtes mail, infrastructure, optimisation des processus.\n"
            "Étapes suivantes en attente.\n"
        ),
    )
    assert quick_classify(msg) == "autre", (
        f"Un mail interne de CDAL doit être classifié 'autre', pas autre chose. "
        f"Got: {quick_classify(msg)!r}"
    )


def test_quick_classify_routes_internal_calendar_invite_to_autre() -> None:
    """#651/#617/#612 — invitations calendrier CDAL → 'autre'."""
    msg = _msg(
        "cdal@digitalhs.biz",
        subject="Updated invitation: CDAL - Daniel D @ Mon 22 Jun 2026 17:00",
        body="You're invited to a meeting.",
    )
    assert quick_classify(msg) == "autre"


def test_quick_classify_routes_wp_contact_form_to_demande_client() -> None:
    """Non-régression — un formulaire WP reste demande_client."""
    msg = _msg(
        "wordpress@detectivebelgique.be",
        subject="Nouveau Message De Détective privé Belgique",
        body=(
            "Bonjour,\nJe vous contacte car je pense que ma conjointe me trompe.\n"
            "Nom: Test\nPrénom: Test\nTéléphone: 0000\n"
            "Votre profil ?: Particulier\n"
        ),
    )
    # Le formulaire WP part avant le check interne (wordpress@ n'est PAS un
    # domaine cabinet), donc → demande_client.
    assert quick_classify(msg) == "demande_client"


# --- Défense en profondeur : generate_draft() ------------------------------


from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.generator import generate_draft


def _make_mailbox() -> MailboxConfig:
    return MailboxConfig(
        name="detective_belgique",
        user="contact@detectivebelgique.be",
        app_password="x",
        brand="Detective Belgique",
        default_lang="fr",
        db_path=Path("./data/boite1.sqlite"),
        imap_host="mail.infomaniak.com",
        imap_port=993,
        short_code="D_FR",
        cerveau2_marque="detectivebelgique",
    )


@pytest.mark.asyncio
async def test_generate_draft_skips_internal_sender(monkeypatch) -> None:
    """#686 — generate_draft doit skip si sender interne, peu importe la category."""
    # Bypass rag (synchrone via to_thread) + vault (async) + case_classifier (async)
    from app.pipeline import generator as gen_module

    def _sync_noop(*args, **kwargs):
        return []

    async def _noop_vault(*args, **kwargs):
        return [], ""

    async def _noop_case(*args, **kwargs):
        return ("infidelite_filature", 0.99, "test")

    monkeypatch.setattr(gen_module, "retrieve", _sync_noop)
    monkeypatch.setattr(gen_module, "query_vault", _noop_vault)
    monkeypatch.setattr(gen_module, "classify_case", _noop_case)

    mb = _make_mailbox()
    result = await generate_draft(
        incoming_subject="Résumé du meeting 260626",
        incoming_body=(
            "Bonjour Daniel\nVoici le résumé de notre meeting.\n"
            "CDAL\n"
        ),
        sender="cdal@digitalhs.biz",
        mailbox=mb,
        language="fr",
        category="demande_client",  # même avec category forcée à demande_client, on skip
    )
    assert result.raw_draft == "", (
        f"Un mail interne ne doit PAS générer de brouillon, "
        f"même si la category est demande_client. Got: {result.raw_draft[:200]!r}"
    )
    assert "interne" in result.note.lower()


@pytest.mark.asyncio
async def test_generate_draft_passes_real_client(monkeypatch) -> None:
    """Non-régression — un vrai client génère bien un brouillon."""
    from app.pipeline import generator as gen_module

    def _sync_noop(*args, **kwargs):
        return []

    async def _noop_vault(*args, **kwargs):
        return [], ""

    async def _noop_case(*args, **kwargs):
        return ("infidelite_filature", 0.99, "test")

    monkeypatch.setattr(gen_module, "retrieve", _sync_noop)
    monkeypatch.setattr(gen_module, "query_vault", _noop_vault)
    monkeypatch.setattr(gen_module, "classify_case", _noop_case)

    mb = _make_mailbox()
    result = await generate_draft(
        incoming_subject="Demande filature",
        incoming_body=(
            "Bonjour,\nJe vous contacte pour une filature.\n"
            "Nom: Dupont\nPrénom: Jean\nTéléphone: 0471234567\n"
            "Votre profil ?: Particulier\n"
        ),
        sender="client.real@example.com",
        mailbox=mb,
        language="fr",
        category="demande_client",
    )
    # Un vrai client DOIT générer un brouillon (pas vide).
    assert result.raw_draft != "", (
        f"Un vrai client doit générer un brouillon. Got empty draft."
    )
    assert result.note == "", f"Pas de note attendue pour un vrai client. Got: {result.note!r}"