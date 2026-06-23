"""Tests unitaires pour le builder de brouillon qualifiant."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import (
    _build_vague_request_draft,
    _extract_first_name,
    _is_vague_request,
    build_followup_ack_draft,
    build_qualification_draft,
    suggested_subject_for_draft,
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
    # Une question de tarif rend la demande non-floue (le client sait ce qu'il
    # veut) → brouillon standard avec toutes les questions manquantes.
    draft = build_qualification_draft(
        subject="Demande",
        body="Bonjour,\n\nQuel est votre tarif pour une filature ?\n\nCordialement,\nPierre Martin\n",
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
        body="Bonjour,\n\nCombien coûte votre intervention ?\n\nCordialement,\nPierre Martin\n",
        sender="pierre@example.com",
        mailbox=mailbox,
        case="incapacite_travail",
    )
    assert "certificat d'incapacité de travail" in draft
    assert "chantier" in draft


def test_build_draft_for_recherche_personne(mailbox: MailboxConfig) -> None:
    draft = build_qualification_draft(
        subject="Disparu",
        body="Bonjour,\n\nQuel est le tarif ?\n\nCordialement,\nSophie Dubois\n",
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


# --- v1.25.1 — #515 : sujet de brouillon lisible + demande floue -----------------


def test_suggested_subject_none_for_pertinent_subject() -> None:
    # Sujet client normal, expéditeur direct → on garde le sujet original.
    assert (
        suggested_subject_for_draft(
            subject="Demande de filature sur mon conjoint",
            body="Bonjour,\n\nmon nom est Jean Dupont\n",
            sender="jean.dupont@gmail.com",
            case="infidelite_filature",
        )
        is None
    )


def test_suggested_subject_for_wp_template_subject() -> None:
    # Sujet template WP absurde relayé par forwarder → libellé cas + nom client.
    subject = "Nouveau Message De Détective privé Belgique - Prenons contact"
    result = suggested_subject_for_draft(
        subject=subject,
        body="Bonjour,\n\nmon nom est Jean Dupont\n",
        sender="contact@detectivebelgique.be",
        case="infidelite_filature",
    )
    assert result == "Filature / surveillance — Jean Dupont"


def test_suggested_subject_for_forwarder_sender_without_template() -> None:
    # Pas de template dans le sujet, mais expéditeur = forwarder WP → sujet absurde.
    result = suggested_subject_for_draft(
        subject="Demande",
        body="Bonjour,\n\nmon nom est Jean Dupont\n",
        sender="WordPress <wordpress@detectivebelgique.be>",
        case="recherche_personne",
    )
    assert result is not None
    assert result.startswith("Recherche de personne — ")


def test_suggested_subject_label_only_when_no_name() -> None:
    # Sujet absurde mais aucun nom extrait du body → libellé seul, sans « — ».
    result = suggested_subject_for_draft(
        subject="Contactformulier",
        body="Bonjour,\n\nQuel est le tarif ?\n",
        sender="contactform@detectivebelgique.be",
        case="incapacite_travail",
    )
    assert result == "Incapacité de travail"
    assert "—" not in (result or "")


def test_is_vague_request_dette_always_false() -> None:
    # La dette a sa propre structure de brouillon, jamais floue.
    assert _is_vague_request("Bonjour", "recuperation_dette", {}, {}) is False


def test_is_vague_request_tariff_question_is_not_vague() -> None:
    assert _is_vague_request(
        "Combien coûte votre intervention ?", "infidelite_filature", {}, {}
    ) is False


def test_is_vague_request_nondetermine_short_is_vague() -> None:
    assert _is_vague_request("Bonjour", "non_determine", {}, {}) is True


def test_is_vague_request_nondetermine_long_is_not_vague() -> None:
    long_body = "Bonjour,\n\n" + ("Je vous contacte pour une affaire délicate. " * 20)
    assert len(long_body) >= 200
    assert _is_vague_request(long_body, "non_determine", {}, {}) is False


def test_is_vague_request_classified_without_op_info_is_vague() -> None:
    # Cas classé mais aucune info opérationnelle (cible, adresse, horaires…).
    assert _is_vague_request("Bonjour", "infidelite_filature", {}, {}) is True


def test_is_vague_request_classified_with_op_info_is_not_vague() -> None:
    # Une info opérationnelle (nom de la cible) suffit à sortir du flou.
    assert (
        _is_vague_request(
            "Bonjour",
            "infidelite_filature",
            {"nom_cible": "Grégory Segers"},
            {},
        )
        is False
    )


def test_build_vague_request_draft_has_clarification_and_tariffs(
    mailbox: MailboxConfig,
) -> None:
    draft = "\n".join(
        _build_vague_request_draft(
            greeting="Bonjour Pierre,",
            first_name="Pierre",
            mailbox=mailbox,
            case="infidelite_filature",
            client_info={},
            case_info={},
        )
    )
    assert "souhaitez obtenir" in draft
    assert "Ouverture de dossier" in draft
    assert "Daniel Hurchon" in draft
    # Pas de questions opérationnelles numérotées tant que la demande est floue.
    assert "1. Vos nom et prénom complets" not in draft
    assert "2." not in draft


def test_build_vague_request_draft_mentions_phone_if_provided(
    mailbox: MailboxConfig,
) -> None:
    # Task #4 : si un téléphone est extrait du body, on propose un rappel.
    draft = "\n".join(
        _build_vague_request_draft(
            greeting="Bonjour,",
            first_name=None,
            mailbox=mailbox,
            case="non_determine",
            client_info={"telephone": "0491502786"},
            case_info={},
        )
    )
    assert "0491502786" in draft
    assert "de vive voix" in draft


def test_build_qualification_draft_vague_request_path(mailbox: MailboxConfig) -> None:
    # Mail lapidaire sans demande opérationnelle ni question de tarif → brouillon flou.
    draft = build_qualification_draft(
        subject="Demande",
        body="Bonjour,\n\nCordialement,\nPierre Martin\n",
        sender="pierre@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "souhaitez obtenir" in draft
    assert "1. Vos nom et prénom complets" not in draft
