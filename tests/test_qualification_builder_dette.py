"""Tests du brouillon spécifique récupération de dette."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import build_qualification_draft


@pytest.fixture
def mailbox() -> MailboxConfig:
    return MailboxConfig(
        name="detective_belgique",
        user="test@detectivebelgique.be",
        app_password="x",
        brand="Detective Belgique",
        default_lang="fr",
        db_path=Path("./data/boite1.sqlite"),
        imap_host="mail.infomaniak.com",
        imap_port=993,
        short_code="D_FR",
        cerveau2_marque="detectivebelgique",
    )


def test_dette_draft_structure(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject=(
            "Enquête sur un membre de mon entourage qui me doit une grosse somme d'argent"
        ),
        body=(
            "Nom: kangudia\n"
            "Prénom: Eunice\n"
            "Téléphone: 0474163904\n"
            "Heure de contact: 10\n"
            "Votre profil ?: Particulier"
        ),
        sender="eunice@example.com",
        mailbox=mailbox,
        case="recuperation_dette",
    )
    assert "Nous accusons bonne réception" in draft
    assert "reconnaissance de dette" in draft
    assert "Identité complète" in draft
    assert "Dernière adresse connue" in draft
    assert "Numéros de téléphone" in draft
    assert "Employeur ou activité professionnelle" in draft
    assert "Biens éventuels" in draft
    assert "stratégie d'intervention adaptée" in draft
    assert "Daniel Hurchon" in draft
    assert "Bien à vous" in draft
    # Les informations déjà reçues doivent être listées comme telles.
    assert "éléments que nous avons bien reçus" in draft
    assert "Vos nom et prénom : Eunice Kangudia" in draft
    assert "Votre GSM : 0474163904" in draft
    assert "Votre email : eunice@example.com" in draft
    assert "Heure de contact souhaitée : 10h" in draft
    assert "Profil : Particulier" in draft
    # Le GSM ne doit pas être redemandé.
    assert "Votre GSM de contact direct" not in draft
    assert "adresse complète et GSM" not in draft
    # On ne doit PAS avoir les questions génériques des autres cas.
    assert "Votre adresse complète (ou société" not in draft
    assert "Nom, prénom et adresse de départ connue" not in draft
    assert "Photo récente" not in draft
    assert "Véhicule de la personne concernée" not in draft
