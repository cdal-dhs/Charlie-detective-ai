from __future__ import annotations

import pytest

from app.workers.imap_poller import _bypass_prefilter_for_followup

"""Tests de la garde anti-faux-négatif post-pré-filtre (v1.25.10).

Le pré-filtre rapide `quick_classify()` est trop rugueux sur les réponses clients :
une bannière "External email / Do not reply" ou un keyword automatique peut
absorber une vraie relance humaine en `autre`. `_bypass_prefilter_for_followup`
permet de renvoyer le mail vers le classifier + post-traitement quand les
heuristiques de relance/réponse humaine sont positives.
"""


_BODY_12377 = (
    "Bonjour,\n"
    "Je vous remercie pour votre e-mail.\n"
    "Je vais voir avec mon responsable la suite à donner et "
    "je vous tiendrai informé.\n"
    "J'ai bien reçu votre message vocal hier. Il n'est pas nécessaire "
    "de programmer un appel aujourd'hui à 9h00, c'est assez clair pour moi.\n"
    "Dans le cas où nous avancerions avec vous, je vous enverrai peut-être "
    "une invitation Teams afin de faire le point également avec ma collègue en copie.\n"
    "Merci encore pour votre réactivité. Je vous tiendrai informé dès que possible.\n"
    "Bien à vous,\n\n"
    "Olivier Léonard\n"
    "Human resources specialist\n"
    "Phone: 0491/33.22.44\n"
    "olivier.leonard@magotteaux.com\n"
    "www.magotteaux.com\n"
)


@pytest.mark.parametrize("prefilter", ["autre", "newsletter", "rappel", "facture"])
def test_bypass_when_human_followup(prefilter: str) -> None:
    """#621/#625 - réponse client avec Re: + body signé + marqueur relance."""
    assert _bypass_prefilter_for_followup(
        prefilter,
        "RE: Nouveau Message De Détective privé Belgique - Prenons contact",
        _BODY_12377,
        "olivier.leonard@magotteaux.com",
    ) is True


def test_bypass_when_reply_to_daniel() -> None:
    """#606 - citation d'un mail de Daniel + préfixe Re:."""
    body = (
        "Bonjour,\n\nJe vous ai répondu en vert sur votre mail\n\n"
        "Bien à vous\n\nFrédéric Van Houtte\n\n"
        "> Le 16 juin 2026 à 15:23, Detective Belgique "
        "<contact@detectivebelgique.be> a écrit :\n"
        ">\n> MISSION : OUVRIER EN INCAPACITÉ DE TRAVAIL\n"
        "> Daniel Hurchon - DetectiveBelgique.be SRL - GSM - 0471/31.81.20"
    )
    assert _bypass_prefilter_for_followup(
        "autre",
        "Re: Mission ouvrier en maladie",
        body,
        "etsvanhoutte@gmail.com",
    ) is True


def test_no_bypass_when_prefilter_is_phishing() -> None:
    """La garde ne s'applique pas au pré-filtre phishing (géré séparément)."""
    assert _bypass_prefilter_for_followup(
        "phishing",
        "RE: Nouveau Message De Détective privé Belgique - Prenons contact",
        _BODY_12377,
        "olivier.leonard@magotteaux.com",
    ) is False


def test_no_bypass_when_not_a_followup() -> None:
    """Vrai email de service automatique sans marqueur relance → pas de bypass."""
    assert _bypass_prefilter_for_followup(
        "autre",
        "Votre reçu de paiement",
        "Merci pour votre paiement. Votre reçu est en pièce jointe.\n--\nService Stripe",
        "receipt@stripe.com",
    ) is False


def test_no_bypass_when_no_prefilter() -> None:
    """Si le pré-filtre ne s'est pas déclenché (None), pas de bypass à décider."""
    assert _bypass_prefilter_for_followup(
        None,
        "RE: Nouveau Message De Détective privé Belgique - Prenons contact",
        _BODY_12377,
        "olivier.leonard@magotteaux.com",
    ) is False
