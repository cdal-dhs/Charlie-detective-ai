"""Tests du durcissement anti-oubli demande_client — v1.22.1.

Bug résolu : mail #504 (zabougafz@gmail.com) avec 'Re: demande d'un détective
pour une personne' + body 'c'est combien le tarif exacte svp' + citation d'un
devis passé → le LLM classifier retournait 'facture' au lieu de 'demande_client'.
Conséquence : pas de brouillon généré, Daniel n'a pas eu de proposition.

Couvre :
- _looks_like_human_question : heuristique Python pure
- _enforce_recall_over_precision : post-traitement qui force demande_client
- classify() : intégration complète (mock LLM)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.classifier import (
    _enforce_recall_over_precision,
    _looks_like_human_question,
    classify,
)


# ── _looks_like_human_question ──────────────────────────────────


def test_looks_human_cas_504_zabougafz():
    """Le mail qui a déclenché le hotfix — doit matcher."""
    body = "c'est combien le tarif exacte svp"
    subject = "Re: demande d'un détective pour une personne"
    sender = "zabougafz@gmail.com"
    assert _looks_like_human_question(body, subject, sender) is True


def test_looks_human_cas_503_breyne_nl():
    """Mail NL 'Vraagje over offerte' — doit matcher."""
    body = "Beste, ik wens een offerte voor een opdracht ivm partneronderzoek."
    subject = "Vraagje over offerte"
    sender = "breyne.toon@gmail.com"
    assert _looks_like_human_question(body, subject, sender) is True


def test_looks_human_question_seul():
    """Un client qui pose juste une question simple, pas de mots-clés enquête."""
    body = "Bonjour, avez-vous reçu mon précédent mail ? Merci"
    subject = "Suivi"
    sender = "client@gmail.com"
    assert _looks_like_human_question(body, subject, sender) is True


def test_looks_human_infidelite_sujet():
    """Sujet qui mentionne infidélité — c'est clairement une demande d'enquête."""
    body = "Bonjour, je pense que mon mari me trompe"
    subject = "Besoin d'aide - suspicion"
    sender = "particulier@gmail.com"
    # 'monsieur/madame' n'est pas matché mais 'Bonjour' + 'mon mari' le sont
    assert _looks_like_human_question(body, subject, sender) is True


def test_looks_human_newsletter_false():
    """Newsletter avec sender commercial — ne doit PAS matcher."""
    body = "Découvrez nos offres spéciales du moment, -50% sur tout"
    subject = "30/06 : Dernier délai pour choisir un partenaire RH"
    sender = "noreply@mail-expert04.be"
    assert _looks_like_human_question(body, subject, sender) is False


def test_looks_human_infomaniak_2fa_false():
    """Mail Infomaniak 2FA — ne doit PAS matcher."""
    body = "Validation en deux étapes activée avec succès"
    subject = "Validation en deux étapes activée"
    sender = "no-reply@infomaniak.com"
    assert _looks_like_human_question(body, subject, sender) is False


def test_looks_human_phishing_noreply_false():
    """Phishing avec sender service — ne doit PAS matcher (phishing a sa catégorie)."""
    body = "Votre compte a été suspendu. Cliquez ici."
    subject = "URGENT - Action requise"
    sender = "noreply@banque-verify.com"
    assert _looks_like_human_question(body, subject, sender) is False


def test_looks_human_body_trop_court():
    """Body quasi-vide — ne doit PAS matcher (trop d'incertitude)."""
    body = "ok"
    subject = "Re:"
    sender = "client@gmail.com"
    assert _looks_like_human_question(body, subject, sender) is False


# ── _enforce_recall_over_precision ─────────────────────────────


def test_recall_override_facture_to_demande():
    """Cas #504 : LLM dit 'facture' mais heuristique humaine → on force demande_client."""
    subject = "Re: demande d'un détective pour une personne"
    body = "c'est combien le tarif exacte svp"
    sender = "zabougafz@gmail.com"
    result = _enforce_recall_over_precision("facture", subject, body, sender)
    assert result == "demande_client"


def test_recall_override_autre_to_demande():
    """LLM dit 'autre' mais heuristique humaine → on force demande_client."""
    subject = "Question rapide"
    body = "Bonjour, je voudrais un devis pour une filature svp"
    sender = "client@gmail.com"
    result = _enforce_recall_over_precision("autre", subject, body, sender)
    assert result == "demande_client"


def test_recall_override_rappel_to_demande():
    """LLM dit 'rappel' mais c'est en fait une demande → on force demande_client."""
    subject = "Suite à votre devis"
    body = "Bonjour, j'attends de vos nouvelles pour confirmer"
    sender = "client@gmail.com"
    result = _enforce_recall_over_precision("rappel", subject, body, sender)
    assert result == "demande_client"


def test_recall_override_demande_kept():
    """Si LLM dit déjà demande_client, on ne change rien."""
    subject = "Question"
    body = "Bonjour"
    sender = "client@gmail.com"
    result = _enforce_recall_over_precision("demande_client", subject, body, sender)
    assert result == "demande_client"


def test_recall_override_newsletter_kept():
    """Newsletter ne doit JAMAIS être remontée en demande_client (trop dangereux)."""
    subject = "Offre spéciale -50%"
    body = "Découvrez nos promotions"
    sender = "newsletter@hubspot.com"
    result = _enforce_recall_over_precision("newsletter", subject, body, sender)
    assert result == "newsletter"


def test_recall_override_phishing_kept():
    """Phishing ne doit JAMAIS être remontée en demande_client (sécurité)."""
    subject = "Compte suspendu"
    body = "Cliquez ici pour vérifier"
    sender = "noreply@banque-verify.com"
    result = _enforce_recall_over_precision("phishing", subject, body, sender)
    assert result == "phishing"


def test_recall_override_service_sender_kept():
    """Sender de service + LLM dit 'autre' → on ne remonte PAS."""
    subject = "votre facture est disponible"
    body = "Veuillez trouver ci-joint votre facture"
    sender = "billing@ovh.com"
    result = _enforce_recall_over_precision("autre", subject, body, sender)
    assert result == "autre"


# ── classify() intégration avec LLM mocké ──────────────────────


@pytest.mark.asyncio
async def test_classify_cas_504_via_llm():
    """Test end-to-end : LLM dit 'facture' → post-traitement force 'demande_client'."""
    subject = "Re: demande d'un détective pour une personne"
    body = "c'est combien le tarif exacte svp"
    sender = "zabougafz@gmail.com"

    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="facture")):
        result = await classify(subject, body, sender)
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_newsletter_pas_override():
    """LLM dit 'newsletter' → on garde 'newsletter' même avec body 'question'."""
    subject = "30/06 : Dernier délai pour choisir un partenaire RH"
    body = "Bonjour à tous, voici la newsletter du mois"
    sender = "noreply@mail-expert04.be"

    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="newsletter")):
        result = await classify(subject, body, sender)
    assert result == "newsletter"


@pytest.mark.asyncio
async def test_classify_invalid_llm_response_fallback():
    """Si LLM retourne du garbage, fallback sur 'autre' + post-traitement."""
    subject = "Question tarif"
    body = "Bonjour, c'est combien ?"
    sender = "client@gmail.com"

    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="je sais pas lol")):
        result = await classify(subject, body, sender)
    # Le garbage est invalide → fallback 'autre' → heuristique humaine True → demande_client
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_already_demande_kept():
    """Si LLM dit déjà demande_client, on ne touche pas."""
    subject = "Question enquête"
    body = "Bonjour, je voudrais une enquête sur..."
    sender = "client@gmail.com"

    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="demande_client")):
        result = await classify(subject, body, sender)
    assert result == "demande_client"
