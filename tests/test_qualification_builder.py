"""Tests unitaires pour le builder de brouillon qualifiant."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import (
    _extract_first_name,
    build_followup_ack_draft,
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


_SOPHIE_BODY = """Bonjour
mon nom est  Bassem Sophie
rue des Déportés 136 6042 Lodelinsart
gsm 0491502786
Segers,Grégory
Brico Fontaine  L'Evêque  : rue de Charleroi 131 6140 Fontaine l'Evêque
semaine du 18 juin travaille 14h à  20h samedi inclus  après celà je ne
sais pas l'adresse ou il va retourne chez sa maîtresse   dort et le
dimanche aussi
semaine du 22 juin travaille  6h à 14h
 chez sa maîtresse et puis travaille
son véhicule est une Bmw grand TOUREUR couleur photo  avec plaque

j'ai besoin une photo quand il rentre chez elle et adresse  s'il a une clé
de son domicile

je voudrais prouver qu'il dort là bas ne fus que directement après son
travail  et qu'il vit avec
 attention petit budget je vis seule avec un de mes enfants  dans le
domicile conjugal

Le mer. 17 juin 2026 à 11:35, contact@detectivebelgique.be <
contact@detectivebelgique.be> a écrit :

> Bonjour Sophie,
>
> Je comprends que vous souhaitez mettre en place une surveillance afin
> d'obtenir des éléments concrets sur une situation qui vous préoccupe.
"""


def test_build_draft_for_sophie_601_filters_answered_questions(mailbox: MailboxConfig) -> None:
    """Mail #601 : Sophie a déjà fourni presque tout, ne pas tout redemander."""
    draft = build_qualification_draft(
        subject="Re: Nouveau Message De Détective privé Belgique - Prenons contact",
        body=_SOPHIE_BODY,
        sender="sososb2810@gmail.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "Bonjour Sophie," in draft
    assert "Grégory Segers" in draft
    assert "rue des Déportés 136" in draft
    assert "0491502786" in draft
    assert "rue de Charleroi 131" in draft
    # Photo non fournie -> doit être redemandée.
    assert "Photo récente" in draft
    # Les éléments déjà reçus ne doivent PAS être redemandés (questions numérotées).
    assert "1. Photo récente" in draft
    assert "2." not in draft  # une seule question manquante
    assert "Vos nom et prénom complets" not in draft
    assert "Votre GSM de contact direct" not in draft
    assert "Votre adresse complète (ou société" not in draft
    assert "Adresse précise de départ" not in draft
    assert "Créneau horaire souhaité" not in draft
    assert "Véhicule de la personne concernée" not in draft


def test_build_followup_ack_draft(mailbox: MailboxConfig) -> None:
    """Brouillon de suivi : merci pour les compléments, pas de requalification."""
    body = "Bonjour Daniel,\n\nVoici la photo et l'adresse complète comme demandé.\n\nCordialement,\nPierre Martin"
    draft = build_followup_ack_draft(
        subject="Re: Demande de filature",
        body=body,
        sender="pierre@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "Bonjour Pierre," in draft
    assert "Merci pour ces compléments d'informations" in draft
    assert "je vous reviens dès que possible" in draft
    assert "estimation de devis fiable" not in draft
    assert "1. Vos nom et prénom complets" not in draft
    assert "Ouverture de dossier : 200 € HTVA" not in draft
    assert "Daniel Hurchon" in draft


def test_build_followup_ack_draft_extracts_first_name_from_quoted_thread(
    mailbox: MailboxConfig,
) -> None:
    """Réponse courte avec thread cité : le prénom doit être extrait du cité."""
    body = """Pour mon prouver et donner à l’avocat


Le jeu. 18 juin 2026, 08:30, Soso Sb <sososb2810@gmail.com> a écrit :

> Bonjour
> mon nom est  Bassem Sophie
> rue des Déportés 136 6042 Lodelinsart
> gsm 0491502786
>
> Le mer. 17 juin 2026 à 11:35, contact@detectivebelgique.be <
> contact@detectivebelgique.be> a écrit :
>
>> Bonjour Sophie,
>>
>> Je comprends que vous souhaitez mettre en place une surveillance
"""
    draft = build_followup_ack_draft(
        subject="Photos",
        body=body,
        sender="sososb2810@gmail.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "Bonjour Sophie," in draft
