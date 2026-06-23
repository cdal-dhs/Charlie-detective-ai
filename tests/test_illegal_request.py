"""Tests de la détection des demandes hors-légalité (v1.24.1 → v1.25.21).

Contexte : mail #614 (Serge M) demande de « faire sortir toutes les
conversations WhatsApp » du téléphone de son épouse = accès non autorisé aux
communications privées = infraction pénale en Belgique. Le brouillon qualifiant
infidélité standard est inadapté : on génère une réponse polie et ferme qui
explique qu'on respecte les lois et propose l'alternative légale (filature /
surveillance / constat).

v1.25.21 — le refus devient un outil de qualification commerciale : en plus du
refus clair, on insiste pour obtenir le but ultime, le contexte et les éléments
disponibles afin de proposer l'alternative légale la plus pertinente.
"""

from __future__ import annotations

import pytest

from app.pipeline.qualification_builder import (
    _detect_illegal_request,
    build_qualification_draft,
)


# Mail #614 — Serge M. Demande hack WhatsApp + infidélité (mixte légal/illégal).
_BODY_614 = (
    "Bonjour,\n\r\n"
    "J 'aimerais prouver l 'infidélité de ma femme qui selon moi dure depuis au moinq 6-8 ans\n\r\n"
    "Elle est en ce moment au congo et je suis certain que son téléphone contient tout les secrets\n\r\n"
    "Est ce que vous pouvez faire sortir toutes les conversations d'au moins 2 ans dans le passé ?\n\r\n"
    "Les enfants ont surpris un message whatsapp ou elle appelait un autre homme \"mon chéri\".\n\r\n"
    "Pouvez vous me dire combien cela va t il coûter et quelles méthodes possédez vous pour "
    "l' attraper la main dans le sac car elle nie absolument tout.\n\r\n"
    "merci\r\n\r\nSerge M"
)


# ── _detect_illegal_request ──────────────────────────────────────


def test_detect_illegal_614_faire_sortir_conversations():
    """#614 : « faire sortir toutes les conversations » doit matcher."""
    match, _ = _detect_illegal_request(_BODY_614)
    assert match is True


def test_detect_illegal_pirater_whatsapp():
    match, _ = _detect_illegal_request("Pouvez-vous pirater son WhatsApp svp ?")
    assert match is True


def test_detect_illegal_acceder_telephone():
    match, _ = _detect_illegal_request("J'aimerais accéder à son téléphone pour voir ses messages.")
    assert match is True


def test_detect_illegal_logiciel_espion():
    match, _ = _detect_illegal_request(
        "Pouvez-vous installer un logiciel espion sur son téléphone sans qu'elle le sache ?"
    )
    assert match is True


def test_detect_illegal_sur_ecoute():
    match, _ = _detect_illegal_request("Je veux la mettre sur écoute téléphonique.")
    assert match is True


def test_detect_illegal_mot_de_passe():
    match, _ = _detect_illegal_request("J'aimerais obtenir le mot de passe de sa boîte mail.")
    assert match is True


def test_detect_illegal_releves_telephoniques():
    match, _ = _detect_illegal_request(
        "Pouvez-vous récupérer les relevés téléphoniques de mon mari ?"
    )
    assert match is True


def test_detect_illegal_via_telephone_number_fr():
    """Retrouver une personne/adresse à partir d'un numéro de GSM = illégal."""
    match, _ = _detect_illegal_request(
        "J'ai son numéro de GSM, pouvez-vous m'obtenir son adresse actuelle ?"
    )
    assert match is True


def test_detect_illegal_savoir_avec_qui_parle():
    """Savoir avec qui la cible communique = interception privée illégale."""
    match, _ = _detect_illegal_request(
        "Je veux savoir avec qui elle parle au téléphone et sur WhatsApp."
    )
    assert match is True


def test_detect_illegal_nl_telefoonnummer():
    match, _ = _detect_illegal_request(
        "Ik wil iemand vinden via zijn telefoonnummer, adres achterhalen."
    )
    assert match is True


def test_detect_illegal_en_phone_number():
    match, _ = _detect_illegal_request(
        "I have her phone number, can you find her current address?"
    )
    assert match is True


def test_detect_illegal_nl_hackeren():
    match, _ = _detect_illegal_request("Kunt u haar telefoon hackeren?")
    assert match is True


def test_detect_illegal_en_hack_into():
    match, _ = _detect_illegal_request("Can you hack into my wife's WhatsApp account?")
    assert match is True


def test_detect_illegal_negative_legit_filature():
    """Une demande légitime de filature ne doit PAS matcher."""
    body = (
        "Bonjour, je pense que mon mari me trompe depuis 2 ans. "
        "Pouvez-vous faire une filature ? Combien ça coûte ? Merci, Marie"
    )
    match, _ = _detect_illegal_request(body)
    assert match is False


def test_detect_illegal_negative_legit_surveillance():
    """Surveillance légale devant le domicile — pas d'illégalité."""
    body = (
        "Bonjour, mon employé est en arrêt maladie mais je le soupçonne de travailler ailleurs. "
        "Pouvez-vous organiser une surveillance devant son domicile ?"
    )
    match, _ = _detect_illegal_request(body)
    assert match is False


def test_detect_illegal_negative_pure_question_tarif():
    match, _ = _detect_illegal_request("Bonjour, quel est votre tarif horaire ?")
    assert match is False


# ── build_qualification_draft — refus poli ─────────────────────


@pytest.mark.asyncio
async def test_build_draft_614_is_refusal_not_standard_qualification():
    """#614 : le brouillon doit être un refus poli (mention lois), pas la
    qualification infidélité standard."""
    from app.config import get_settings

    settings = get_settings()
    mailbox = settings.mailboxes()[0]
    draft = build_qualification_draft(
        subject="іtѕⅿе-Bеvеіlіgіngѕmеldіng: սw dіеnѕt ѕtорgеzеt",
        body=_BODY_614,
        sender="serge@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    # Le refus poli mentionne les lois / infractions pénales.
    assert "infractions pénales" in draft
    assert "respecter scrupuleusement la loi" in draft
    # Le brouillon ne reformule PAS la demande de piratage (« faire sortir »).
    assert "faire sortir" not in draft.lower()
    # Il propose l'alternative légale (filature / surveillance).
    assert "filature" in draft.lower() or "surveillance" in draft.lower()
    # Tarifs présents (transparence).
    assert "Ouverture de dossier" in draft
    # Signature de Daniel.
    assert "Daniel Hurchon" in draft


def test_build_draft_legit_not_refusal():
    """Une demande légitime ne déclenche PAS le refus poli."""
    from app.config import get_settings

    settings = get_settings()
    mailbox = settings.mailboxes()[0]
    body = (
        "Bonjour, je pense que mon mari me trompe. "
        "Pouvez-vous faire une filature ? Combien ça coûte ? Merci, Marie"
    )
    draft = build_qualification_draft(
        subject="Suspicion infidélité",
        body=body,
        sender="marie@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    assert "infractions pénales" not in draft


@pytest.mark.asyncio
async def test_build_draft_includes_qualification_questions():
    """v1.25.21 : le refus illégal doit poser les questions de qualification."""
    from app.config import get_settings

    settings = get_settings()
    mailbox = settings.mailboxes()[0]
    draft = build_qualification_draft(
        subject="WhatsApp",
        body="Pouvez-vous pirater son téléphone pour lire ses messages WhatsApp ?",
        sender="jean@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    # Refus clair + cadre légal.
    assert "infractions pénales" in draft
    assert "piratage" in draft.lower() or "accéder" in draft.lower()
    # Objectif final.
    assert "objectif final" in draft.lower()
    # Lien avec la personne.
    assert "lien avec la personne" in draft.lower()
    # Type d'investigation légale.
    assert "surveillance" in draft.lower()
    # Mention tarifaire reste présente (transparence).
    assert "Ouverture de dossier" in draft
