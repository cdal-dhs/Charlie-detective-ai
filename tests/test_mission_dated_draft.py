"""Tests TDD pour la brique 'mission datée' (v1.28.0) — cf. plan /Users/cdal/.claude/plans/ethereal-foraging-marble.md.

Couvre les root causes RC1-RC5 identifiées sur le mail #672 :
- RC1 : reconnaissance fiancé(e) / compagne/on dans _extract_case_info
- RC2 : élargissement _OPERATIONAL_SIGNAL_RE pour "mission le JJ/MM"
- RC3 + RC4 : extraction date_cible + ville_cible + affichage dans éléments reçus
- RC5 : nouvelle fonction _build_mission_dated_draft (intro capacité+date+réserve,
         urgence FR, clôture "Dans l'attente de votre retour", signature SRL)

TDD : tests écrits AVANT le code de production. Ils doivent échouer à l'état initial
puis passer après implémentation de la brique mission datée.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import MailboxConfig
from app.pipeline.qualification_builder import (
    _OPERATIONAL_SIGNAL_RE,
    _extract_case_info,
    _format_received_info,
    build_qualification_draft,
)


# --- Fixtures ---------------------------------------------------------------

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


@pytest.fixture
def kirara_fixture() -> dict:
    """Charge la fixture anonymisée du mail #672 (Kirara — filature fiancée datée 02/07 Tournai)."""
    p = Path(__file__).parent / "fixtures" / "mail_672_kirara.json"
    with open(p) as f:
        return json.load(f)


# --- RC1 : reconnaissance fiancé(e) / compagne/on ----------------------------


def test_extract_case_info_recognizes_fiancee() -> None:
    """RC1 — 'ma fiancée Marie Dupont' doit peupler prenom_cible/nom_cible."""
    body = (
        "Bonjour,\n"
        "Je cherche un service de filature de ma fiancée Marie Dupont "
        "habitant à Tournai.\nMerci\nNom: Test\nPrénom: Test\n"
        "Téléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("prenom_cible") == "Marie", f"prenom_cible = {info.get('prenom_cible')!r}"
    assert info.get("nom_cible") == "Dupont", f"nom_cible = {info.get('nom_cible')!r}"


def test_extract_case_info_recognizes_fiance_masculin() -> None:
    """RC1 — 'mon fiancé Pierre Martin' (cible masculine) doit être extrait."""
    body = (
        "Bonjour,\n"
        "Je suspecte mon fiancé Pierre Martin de me tromper.\n"
        "Merci\nNom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("prenom_cible") == "Pierre", f"prenom_cible = {info.get('prenom_cible')!r}"
    assert info.get("nom_cible") == "Martin", f"nom_cible = {info.get('nom_cible')!r}"


def test_extract_case_info_recognizes_compagne() -> None:
    """RC1 — 'ma compagne Sophie Lambert' doit être reconnu comme relation cible."""
    body = (
        "Bonjour,\n"
        "Je souhaite une filature de ma compagne Sophie Lambert.\n"
        "Merci\nNom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("prenom_cible") == "Sophie", f"prenom_cible = {info.get('prenom_cible')!r}"
    assert info.get("nom_cible") == "Lambert", f"nom_cible = {info.get('nom_cible')!r}"


# --- RC2 : élargissement _OPERATIONAL_SIGNAL_RE ------------------------------


def test_operational_signal_matches_mission_le_02_juillet() -> None:
    """RC2 — 'filature le 02 juillet' doit matcher (formulation naturelle, pas 'prévue pour')."""
    assert _OPERATIONAL_SIGNAL_RE.search("Je demande une filature le 02 juillet à Bruxelles")


def test_operational_signal_matches_mission_le_02_07() -> None:
    """RC2 — format court JJ/MM doit aussi matcher."""
    assert _OPERATIONAL_SIGNAL_RE.search("mission de filature le 02/07")


def test_operational_signal_matches_durant_weekend() -> None:
    """RC2 — 'durant le week-end du 5 juillet' doit matcher."""
    assert _OPERATIONAL_SIGNAL_RE.search(
        "Je souhaite une surveillance durant le week-end du 5 juillet 2026"
    )


# --- RC3 + RC4 : extraction date_cible / ville_cible + affichage ------------


def test_extract_case_info_extracts_mission_date_short() -> None:
    """RC3 — 'le 02/07' doit être stocké dans case_info['date_cible']."""
    body = (
        "Bonjour,\n"
        "Je cherche un service de filature de ma fiancée Marie Dupont "
        "habitant à Tournai. Le 02/07 elle doit aller à Bruxelles.\n"
        "Merci\nNom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("date_cible"), f"date_cible = {info.get('date_cible')!r}"
    assert "02/07" in info["date_cible"]


def test_extract_case_info_extracts_mission_date_longue() -> None:
    """RC3 — format 'le 15 août 2026' doit aussi être détecté."""
    body = (
        "Bonjour,\nJe cherche une surveillance le 15 août 2026 pour ma conjointe.\n"
        "Merci\nNom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("date_cible"), f"date_cible = {info.get('date_cible')!r}"
    assert "15 août 2026" in info["date_cible"]


def test_extract_case_info_extracts_mission_city() -> None:
    """RC4 — 'à Tournai' doit être stocké dans case_info['ville_cible']."""
    body = (
        "Bonjour,\nJe cherche un service de filature de ma fiancée Marie Dupont "
        "habitant à Tournai. Le 02/07 elle doit aller à Bruxelles.\n"
        "Merci\nNom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    info = _extract_case_info(body, "infidelite_filature")
    assert info.get("ville_cible"), f"ville_cible = {info.get('ville_cible')!r}"
    # La ville de résidence ET la ville de destination peuvent matcher — on accepte l'un ou l'autre.
    assert "Tournai" in info["ville_cible"] or "Bruxelles" in info["ville_cible"]


def test_format_received_info_includes_date_and_city_for_mission_dated() -> None:
    """RC4 — _format_received_info doit afficher 'Date de mission souhaitée' + 'Ville' quand détectées."""
    case_info = {
        "date_cible": "02/07",
        "ville_cible": "Tournai",
        "adresse_depart_cible": "Tournai",
    }
    client_info = {
        "telephone": "0033 7 66 36 41 90",
        "email": "test@example.com",
        "prenom": "Jean",
        "nom": "Dupont",
        "profil": "Particulier",
    }
    lines = _format_received_info(client_info, case_info, "infidelite_filature")
    text = "\n".join(lines)
    assert "Date de mission" in text or "date de mission" in text.lower(), (
        f"Lignes formatées:\n{text}"
    )
    assert "Tournai" in text, f"Lignes formatées:\n{text}"


# --- RC5 : nouvelle brique _build_mission_dated_draft -----------------------


def test_build_qualification_draft_kirara_produces_brouillon_daniel(kirara_fixture, mailbox) -> None:
    """RC5 — Mail #672 Kirara doit produire un brouillon conforme au modèle Daniel."""
    draft = build_qualification_draft(
        subject=kirara_fixture["subject"],
        body=kirara_fixture["body"],
        sender=kirara_fixture["sender"],
        mailbox=mailbox,
        case=kirara_fixture["case"],
    )
    for must_contain in kirara_fixture["expected_draft_must_contain"]:
        assert must_contain in draft, (
            f"Le brouillon devrait contenir {must_contain!r}.\n"
            f"--- Brouillon produit ---\n{draft}\n---"
        )
    for must_not_contain in kirara_fixture["expected_draft_must_NOT_contain"]:
        assert must_not_contain not in draft, (
            f"Le brouillon NE devrait PAS contenir {must_not_contain!r}.\n"
            f"--- Brouillon produit ---\n{draft}\n---"
        )


def test_build_qualification_draft_kirara_no_vague_request(kirara_fixture, mailbox) -> None:
    """RC5 — Le brouillon Kirara ne doit PAS contenir la formulation vague 'que souhaitez-vous'."""
    draft = build_qualification_draft(
        subject=kirara_fixture["subject"],
        body=kirara_fixture["body"],
        sender=kirara_fixture["sender"],
        mailbox=mailbox,
        case=kirara_fixture["case"],
    )
    lowered = draft.lower()
    assert "que souhaitez-vous" not in lowered
    assert "souhaitez-vous obtenir concrètement" not in lowered
    assert "préciser ce que vous" not in lowered


def test_build_qualification_draft_kirara_questions_filtered(kirara_fixture, mailbox) -> None:
    """RC5 — Le brouillon ne doit PAS redemander nom/prénom/GSM/profil (déjà reçus)."""
    draft = build_qualification_draft(
        subject=kirara_fixture["subject"],
        body=kirara_fixture["body"],
        sender=kirara_fixture["sender"],
        mailbox=mailbox,
        case=kirara_fixture["case"],
    )
    # Les éléments déjà reçus par formulaire ne doivent pas être re-demandés.
    # Patterns typiques de questions déjà filtrées : "Vos nom et prénom complets",
    # "Votre GSM de contact direct", "Votre adresse complète".
    assert "Vos nom et prénom complets" not in draft, (
        "La question 'Vos nom et prénom complets' ne doit pas apparaître (déjà reçu)."
    )
    assert "Votre GSM de contact direct" not in draft, (
        "La question 'Votre GSM de contact direct' ne doit pas apparaître (déjà reçu)."
    )
    assert "Votre adresse complète" not in draft, (
        "La question 'Votre adresse complète' ne doit pas apparaître (déjà reçu)."
    )


def test_build_qualification_draft_kirara_signature_srl(kirara_fixture, mailbox) -> None:
    """RC5 — Signature SRL obligatoire en fin de brouillon."""
    draft = build_qualification_draft(
        subject=kirara_fixture["subject"],
        body=kirara_fixture["body"],
        sender=kirara_fixture["sender"],
        mailbox=mailbox,
        case=kirara_fixture["case"],
    )
    assert "Detective Belgique" in draft, "Brand Detective Belgique doit apparaître."
    assert "GSM 0471/31.81.20" in draft, "GSM signature obligatoire."
    assert "contact@detectivebelgique.be" in draft, "Email signature obligatoire."
    assert "Bien à vous" in draft, "Formule de clôture obligatoire."


def test_build_qualification_draft_kirara_urgence_date_proche(kirara_fixture, mailbox) -> None:
    """RC5 — Date 02/07 = proche (< 90j du 2026-06-29) → phrase urgence FR doit apparaître."""
    draft = build_qualification_draft(
        subject=kirara_fixture["subject"],
        body=kirara_fixture["body"],
        sender=kirara_fixture["sender"],
        mailbox=mailbox,
        case=kirara_fixture["case"],
    )
    lowered = draft.lower()
    assert "urgent" in lowered or "urgence" in lowered, (
        f"Le brouillon doit signaler l'urgence pour une mission datée proche.\n{draft}"
    )


# --- Test non-régression : mail non daté doit toujours passer par le builder standard ---


def test_non_dated_mail_passes_through_standard_draft(mailbox) -> None:
    """Garde-fou — un mail vague/non daté doit toujours passer par _build_standard_draft (ou vague_request)."""
    body = (
        "Bonjour,\nJe vous contacte car je pense que ma conjointe me trompe. "
        "Pourriez-vous me recontacter ?\nMerci\n"
        "Nom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    # Pas de date précise → doit déclencher le builder vague_request ou standard (PAS mission_dated).
    draft = build_qualification_draft(
        subject="Question",
        body=body,
        sender="test@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    # La phrase 'Nous pouvons effectivement organiser' doit être ABSENTE (pas de date détectée).
    assert "Nous pouvons effectivement organiser" not in draft, (
        "Un mail sans date précise ne doit PAS déclencher le builder mission_dated.\n"
        f"Brouillon:\n{draft}"
    )


def test_vague_date_does_not_trigger_mission_dated(mailbox) -> None:
    """Garde-fou — formulation vague « durant cet été 2026 » (cas #656 Jennifer Das)
    ne doit PAS basculer dans le builder mission datée (sinon perte du wording
    « dossier de votre client »). Doit passer par le builder standard.
    """
    body = (
        "Je vous adresse la présente en ma qualité de conseil d'un client qui "
        "souhaiterait faire éventuellement appel à vos services dans le cadre de "
        "son divorce. La mission se déroulerait durant cet été 2026.\n\n"
        "Bien dévouée,\n\nMaître Test\n"
        "Nom: Test\nPrénom: Test\nTéléphone: 0000\n"
    )
    draft = build_qualification_draft(
        subject="Question",
        body=body,
        sender="test@example.com",
        mailbox=mailbox,
        case="infidelite_filature",
    )
    # Le builder mission_dated ne doit PAS s'activer (formulation trop vague).
    assert "Nous pouvons effectivement organiser" not in draft, (
        "Un mail avec formulation vague 'durant cet été' ne doit PAS déclencher "
        "le builder mission_dated.\n"
        f"Brouillon:\n{draft}"
    )
    # Et le wording « dossier de votre client » doit être présent (car is_legal_counsel).
    # NB : ce test ne valide pas _is_legal_counsel_email directement, juste qu'on n'est
    # pas dans le builder mission_dated. Le wording avocat est testé ailleurs.