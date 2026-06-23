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
    _has_strong_human_demand,
    _is_human_followup,
    _is_job_application,
    _is_reply_to_daniel,
    _is_wp_contact_form,
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


# ── v1.24.0 — formulaires WordPress, réponses à Daniel, demande forte ──────
# Backport des 3 clients ratés remontés par Daniel le 2026-06-22.


# Mail #515 — Nathalie Hairemans (jalousie / sa nicht), formulaire NL WordPress
# avec sujet trompeur « Réinitialisation du mot de passe » (template WP mal configuré).
_BODY_515 = """Achternaam: Hairemans
Voornaam: Nathalie
Telefoonnummer: 0468287587
Tijdsstippen: 15u
Uw profiel ?: Particulier
Hoe kunnen wij u helpen ? Vertel ons meer: Beste, dit is eigenlijk een speciaal verhaal. Ik ben al 16j samen met mijn man. Ik heb een nicht dat al heel mijn leven in mijn schaduw loopt. Zij probeert al heel ons leven een competitie tussen ons te voeren. Er is veel jaloezie langs haar kant.
Wettelijke kennisgeving & privacybeleid: Dit formulier stelt ons in staat de gegevens te verzamelen."""


def test_is_wp_contact_form_515_nl():
    """Le formulaire NL de #515 doit être détecté."""
    assert _is_wp_contact_form(_BODY_515) is True


def test_recall_override_515_wp_form_from_facture():
    """#515 : LLM dit 'facture' (leurre sujet reset password) → formulaire WP force demande_client."""
    subject = "[Privédetective België] Réinitialisation du mot de passe"
    sender = "wordpress@detectivebelgium.com"
    result = _enforce_recall_over_precision("facture", subject, _BODY_515, sender)
    assert result == "demande_client"


def test_recall_override_515_wp_form_from_phishing():
    """Un formulaire WP ne peut jamais être un phishing — on remonte même depuis phishing."""
    subject = "[Privédetective België] Réinitialisation du mot de passe"
    sender = "wordpress@detectivebelgium.com"
    result = _enforce_recall_over_precision("phishing", subject, _BODY_515, sender)
    assert result == "demande_client"


def test_is_wp_contact_form_615_fr():
    """Formulaire FR detectivebelgique.be (#615) — message en haut + champs Nom/Prénom."""
    body = """bonjour j'habite actuellement en espagne mais dans une semaine je reviens en belgique  j'aurai besoin d'un détective pour faire une petite enquete au bureau de douane de Kaiserslautern est ce que vous accepteriez de le faire ? merci
Nom: scurbecq
Prénom: andree marie
Téléphone: 0484636111
Heure de contact: neant
Votre profil ?: Particulier
Mentions légales & Politique de Confidentialité: Ce formulaire nous permet de collecter."""
    assert _is_wp_contact_form(body) is True


def test_is_wp_contact_form_negative_pure_text():
    """Un mail texte libre sans champs formulaire ne doit PAS matcher."""
    body = "Bonjour, j'aimerais un devis pour une filature. Merci."
    assert _is_wp_contact_form(body) is False


# Mail #606 — Frédéric Van Houtte (follow-up avec coordonnées), Re: + citation du
# devis de Daniel. Le LLM voit « devis », « facturer », « HTVA » dans la citation et
# classe 'facture'. Mais c'est une vraie demande client (le client fournit TVA, GSM).
_BODY_606 = """Bonjour Monsieur

Je vous ai répondu en vert sur votre mail

Bien à vous

Frédéric Van Houtte

GSM : +32(0)483 047 356
Email : etsvanhoutte@gmail.com


> Le 16 juin 2026 à 15:23, Detective Belgique <contact@detectivebelgique.be> a écrit :
>
> MISSION : OUVRIER EN INCAPACITÉ DE TRAVAIL AVEC SUSPICION DE TRAVAILLER AILLEURS
> Notre devis : Ouverture de dossier : 200€ HTVA. Provision : 1263.24€ TVAC
>
> DétectiveBelgique.be SRL
> Daniel Hurchon Détective Privé
> Siège Social : Chaussée Bara 213, 1410 Waterloo
> Autorisation ministérielle – 14.0625.12
> GSM – 0471/31.81.20
> E-mail – contact@detectivebelgique.be
"""


def test_is_reply_to_daniel_606():
    """#606 : Re: + citation signée Daniel + expéditeur humain gmail."""
    assert _is_reply_to_daniel(_BODY_606, "etsvanhoutte@gmail.com") is True


def test_is_reply_to_daniel_wp_forwarder_with_daniel_signature():
    """v1.25.11 - forwarder WP avec citation Daniel = réponse humaine (#513)."""
    assert _is_reply_to_daniel(_BODY_606, "mail@detectivebelgium.com") is True


def test_is_reply_to_daniel_wp_forwarder_no_daniel_signature():
    """Forwarder WP sans citation Daniel → pas une réponse humaine."""
    body = "Beste, ik wens een offerte. Met vriendelijke groeten, Toon"
    assert _is_reply_to_daniel(body, "wordpress@detectivebelgium.com") is False


def test_is_reply_to_daniel_negative_no_daniel_signature():
    """Une citation sans signature Daniel ne déclenche pas la règle."""
    body = "> Le devis est prêt. Cordialement."
    assert _is_reply_to_daniel(body, "client@gmail.com") is False


def test_recall_override_606_reply_to_daniel_from_facture():
    """#606 : LLM dit 'facture' → Re:+citation Daniel force demande_client."""
    subject = "Re: Mission ouvrier en maladie"
    sender = "etsvanhoutte@gmail.com"
    result = _enforce_recall_over_precision("facture", subject, _BODY_606, sender)
    assert result == "demande_client"


# Mail #614 — Serge M (infidélité / filature Congo-WhatsApp). Sujet = homoglyphes
# itsme (« іtѕⅿе-Bеvеіlіgіngѕmеldіng »). Le LLM classe 'phishing' à cause du sujet.
# Mais le body est une vraie demande humaine directe (prénom signé + vocabulaire
# enquête + question de tarif, sans marqueur phishing actif).
_BODY_614 = (
    "Bonjour,\n\r\n"
    "J 'aimerais prouver l 'infidélité de ma femme qui selon moi dure depuis au moinq 6-8 ans\n\r\n"
    "Elle est en ce moment au congo et je suis certain que son téléphone contient tout les secrets\n\r\n"
    "Est ce que vous pouvez faire sortir toutes les conversations d'au moins 2 ans dans le passé ?\n\r\n"
    'Les enfants ont surpris un message whatsapp ou elle appelait un autre homme "mon chéri".\n\r\n'
    "Pouvez vous me dire combien cela va t il coûter et quelles méthodes possédez vous pour "
    "l' attraper la main dans le sac car elle nie absolument tout.\n\r\n"
    "merci\r\n\r\nSerge M"
)


def test_has_strong_human_demand_614():
    """#614 : demande humaine forte — prénom signé + infidélité + combien coûte."""
    match, reason = _has_strong_human_demand(_BODY_614)
    assert match is True, f"attendu True, raison refus: {reason}"


def test_has_strong_human_demand_negative_real_phishing():
    """Un vrai phishing itsme avec CTA d'urgence ne doit PAS matcher."""
    body = (
        "itsme-Beveiligingsmelding: uw dienst stopgezet. "
        "votre compte a été suspendu. Cliquez ici pour vérifier votre identité."
    )
    match, _ = _has_strong_human_demand(body)
    assert match is False


def test_has_strong_human_demand_negative_no_signed_name():
    """Demande sans prénom signé → pas assez fort pour remonter depuis phishing."""
    body = "Bonjour, je veux prouver l'infidélité. Combien ça coûte ?"
    match, _ = _has_strong_human_demand(body)
    assert match is False


def test_has_strong_human_demand_negative_no_price():
    """Vocabulaire enquête + prénom mais pas de question tarif → pas assez fort."""
    body = "Bonjour, je soupçonne mon mari. Cordialement, Marie."
    match, _ = _has_strong_human_demand(body)
    assert match is False


def test_recall_override_614_strong_demand_from_phishing():
    """#614 : LLM dit 'phishing' → demande humaine forte force demande_client."""
    subject = "іtѕⅿе-Bеvеіlіgіngѕmеldіng: սw dіеnѕt ѕtорgеzеt"
    sender = "yashwantsharma@colorsofindiatours.com"
    result = _enforce_recall_over_precision("phishing", subject, _BODY_614, sender)
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_515_via_llm():
    """End-to-end #515 : LLM dit 'facture' → formulaire WP force demande_client."""
    subject = "[Privédetective België] Réinitialisation du mot de passe"
    sender = "wordpress@detectivebelgium.com"
    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="facture")):
        result = await classify(subject, _BODY_515, sender)
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_614_via_llm():
    """End-to-end #614 : LLM dit 'phishing' → demande forte force demande_client."""
    subject = "іtѕⅿе-Bеvеіlіgіngѕmеldіng: սw dіеnѕt ѕtорgеzеt"
    sender = "yashwantsharma@colorsofindiatours.com"
    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="phishing")):
        result = await classify(subject, _BODY_614, sender)
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_real_phishing_kept():
    """Anti-régression : un vrai phishing itsme reste phishing."""
    subject = "іtѕⅿе-Bеvеіlіgіngѕmеldіng: uw dienst stopgezet"
    sender = "noreply@itsme.be"
    body = (
        "itsme-Beveiligingsmelding: uw dienst stopgezet. "
        "votre compte a été suspendu. Cliquez ici pour vérifier votre identité."
    )
    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="phishing")):
        result = await classify(subject, body, sender)
    assert result == "phishing"


# ── v1.25.8 — relance humaine + candidature spontanée ───────────


# Mail Vacature : relance NL d'un candidat après l'email initial.
_BODY_VACATURE_FOLLOWUP = """Beste,

Heeft u mijn e-mail goed ontvangen?

Mvg,

Xavier Plaghki"""


# Mail initial de candidature (si jamais traité seul).
_BODY_VACATURE_INITIAL = """Beste,

Zijn er opties om als zelfstandige bij jullie te werken in bijberoep?

Mvg,

Xavier Plaghki
04715633335"""


def test_is_human_followup_vacature():
    """#Vacature : 'Rép.: Vacature' + relance NL signée = follow-up humain."""
    assert (
        _is_human_followup("Rép.: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com")
        is True
    )


def test_is_human_followup_vacature_dutch_prefix():
    """Même relance avec préfixe NL 'AW:' doit matcher."""
    assert (
        _is_human_followup("AW: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com")
        is True
    )


def test_is_human_followup_newsletter_commercial_false():
    """Newsletter commerciale classique — pas un follow-up humain."""
    assert (
        _is_human_followup(
            "Offre spéciale -50%", "Découvrez nos promotions", "newsletter@hubspot.com"
        )
        is False
    )


def test_is_job_application_vacature_initial():
    """Candidature spontanée NL signée = job application."""
    assert (
        _is_job_application("Vacature", _BODY_VACATURE_INITIAL, "xavierplaghki@hotmail.com") is True
    )


def test_is_job_application_newsletter_sender_false():
    """Job-board qui envoie depuis un sender marketing n'est pas un humain."""
    body = "Vacature bij ons! Solliciteer nu.\n\nTeam HR"
    assert _is_job_application("Vacature detectieve", body, "newsletter@jobs.be") is False


def test_recall_override_vacature_from_newsletter():
    """#Vacature : LLM dit 'newsletter' → relance humaine force demande_client."""
    result = _enforce_recall_over_precision(
        "newsletter", "Rép.: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com"
    )
    assert result == "demande_client"


def test_recall_override_vacature_from_autre():
    """#Vacature : LLM dit 'autre' → relance humaine force demande_client."""
    result = _enforce_recall_over_precision(
        "autre", "Rép.: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com"
    )
    assert result == "demande_client"


def test_recall_override_vacature_from_phishing_kept():
    """Anti-régression : une relance classée 'phishing' par le LLM reste phishing."""
    result = _enforce_recall_over_precision(
        "phishing", "Rép.: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com"
    )
    assert result == "phishing"


def test_recall_override_vacature_job_from_newsletter():
    """Candidature spontanée classée 'newsletter' → job_application force demande_client."""
    result = _enforce_recall_over_precision(
        "newsletter", "Vacature", _BODY_VACATURE_INITIAL, "xavierplaghki@hotmail.com"
    )
    assert result == "demande_client"


@pytest.mark.asyncio
async def test_classify_vacature_from_newsletter():
    """End-to-end #Vacature : LLM dit 'newsletter' → post-traitement force demande_client."""
    with patch("app.pipeline.classifier.complete", new=AsyncMock(return_value="newsletter")):
        result = await classify(
            "Rép.: Vacature", _BODY_VACATURE_FOLLOWUP, "xavierplaghki@hotmail.com"
        )
    assert result == "demande_client"
