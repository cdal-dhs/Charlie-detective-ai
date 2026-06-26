"""Tests du brouillon spécifique investigation patrimoniale / succession.

v1.25.27 — cf. #643 (Boeteman) : le client veut connaître l'ampleur d'une
succession et réserver ses droits d'héritier. Le brouillon dédié pose les
bonnes questions succession et NE redemande PAS l'objectif (le brouillon
« demande floue » générique était déclenché à tort avant le fix).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import (
    _is_vague_request,
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
        imap_host="mail.infomaniak.com",
        imap_port=993,
        short_code="D_FR",
        cerveau2_marque="detectivebelgique",
    )


_BODY_643 = (
    "le père de ma femme serait mourant (plus de contact depuis 10 ans) , "
    "ma compagne est sa seule héritière directe.  Il serait soigné à Saint Luc "
    "à Bruxelles, il est ex-diplomate belge habite la France et Madagascar.  "
    "Nous aimerions connaître l'ampleur de sa succession et réserver nos droits "
    "le cas échéant.\n"
    "Nom: Boeteman\n"
    "Prénom: Philippe\n"
    "Téléphone: 0488726161\n"
    "Heure de contact: 8-22\n"
    "Votre profil ?: Particulier\n"
    "Mentions légales & Politique de Confidentialité: Ce formulaire nous permet..."
)


def test_succession_draft_structure(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject="Nouveau Message De Détective privé Belgique - Prenons contact",
        body=_BODY_643,
        sender="phboeteman@hotmail.com",
        mailbox=mailbox,
        case="investigation_successorale",
    )
    # Accusé réception spécifique succession.
    assert "Nous accusons bonne réception" in draft
    assert "réservation de vos droits d'héritier" in draft
    # Les 8 questions succession doivent être présentes.
    assert "Identité complète de la personne concernée" in draft
    assert "État actuel, date et lieu du décès" in draft
    assert "Dernière adresse connue de la personne" in draft
    assert "Lien de parenté de l'héritier" in draft
    assert "Nationalité et statut de la personne" in draft
    assert "Notaire déjà contacté" in draft
    assert "Banques, comptes, biens immobiliers" in draft
    assert "Existence d'un testament connu" in draft
    # Closing + signature Daniel.
    assert "stratégie d'intervention adaptée" in draft
    assert "notaire compétent" in draft
    assert "Daniel Hurchon" in draft
    assert "Bien à vous" in draft
    # Infos client déjà reçues restituées.
    assert "éléments que nous avons bien reçus" in draft
    assert "Vos nom et prénom : Philippe Boeteman" in draft
    assert "Votre GSM : 0488726161" in draft
    assert "Votre email : phboeteman@hotmail.com" in draft
    assert "Heure de contact souhaitée : 8-22" in draft
    # Éléments succession extraits du message libre et restitués (pas redemandés).
    assert "Personne concernée : père de ma femme" in draft
    assert "Lieu de soins : Saint Luc" in draft
    assert "ex-diplomate" in draft
    # CRITIQUE — le brouillon flou ne doit PAS être déclenché : on ne redemande
    # jamais l'objectif (le client l'a déjà exprimé).
    assert "souhaitez obtenir" not in draft
    assert "ce que vous souhaitez obtenir concrètement" not in draft
    # Pas de questions génériques des autres cas.
    assert "Photo récente" not in draft
    assert "Véhicule de la personne concernée" not in draft
    assert "créneau horaire souhaité" not in draft
    # Pas de tarifs pour ce cas dédié (comme la dette — stratégie après éléments).
    assert "Ouverture de dossier" not in draft


def test_is_vague_request_excludes_succession() -> None:
    """v1.25.27 — investigation_successorale est exclue du flou : son brouillon
    dédié pose les questions d'office, on ne tombe JAMAIS dans la clarification
    générique qui redemande l'objectif."""
    assert _is_vague_request(
        _BODY_643,
        "investigation_successorale",
        case_info={},
        client_info={},
        objective_clear=False,
    ) is False
