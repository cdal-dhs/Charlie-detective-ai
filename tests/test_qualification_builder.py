"""Tests unitaires pour le builder de brouillon qualifiant."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import (
    _extract_first_name,
    build_qualification_draft,
)


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


def test_extract_first_name_after_signoff() -> None:
    body = "Bonjour,\n\nJe souhaite une filature.\n\nCordialement,\nChristophe Dupont\nDirecteur"
    assert _extract_first_name(body) == "Christophe"


def test_extract_first_name_rejects_titles() -> None:
    body = "Merci.\n\nBien à vous,\n\nDirecteur des opérations"
    assert _extract_first_name(body) is None


def test_extract_first_name_rejects_single_word() -> None:
    body = "Salut,\n\nJean"
    assert _extract_first_name(body) is None


def test_build_draft_contains_base_questions(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject="Demande",
        body="Bonjour,\n\nCordialement,\nPierre Martin\n",
        sender="pierre@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "Bonjour Pierre," in draft
    assert "1. Vos nom et prénom complets." in draft
    assert "9." in draft
    assert "Ouverture de dossier : 200 € HTVA." in draft
    assert " deux détectives" in draft
    assert "je reprendrai contact" in draft
    assert "échange téléphonique" in draft
    assert "estimation de devis fiable" in draft
    assert "Daniel Hurchon" in draft


def test_build_draft_for_incapacite_travail(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject="Arrêt maladie",
        body="Bonjour,\n\nCordialement,\nPierre Martin\n",
        sender="pierre@example.com",
        mailbox=mailbox,
        case="incapacite_travail",
    )
    assert "certificat d'incapacité de travail" in draft
    assert "chantier" in draft


def test_build_draft_for_recherche_personne(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject="Disparu",
        body="Bonjour,\n\nCordialement,\nSophie Dubois\n",
        sender="sophie@example.com",
        mailbox=mailbox,
        case="recherche_personne",
    )
    assert "Date de naissance exacte" in draft
    assert "Belgique, France, Luxembourg" in draft
