"""Tests de la détection citation Daniel au format Outlook (v1.25.11).

Le mail #513 (Toon Breyne, `wordpress@detectivebelgium.com`) est une réponse
client qui cite un mail de Daniel, mais au format texte Outlook NL/FR
(Van:/Verzonden:/Aan:/Onderwerp: et De:/Date:/À:/Objet:) au lieu du préfixe
`>` classique. L'expéditeur technique est un forwarder WordPress.
"""

from __future__ import annotations

from app.pipeline.classifier import _body_quotes_daniel, _is_reply_to_daniel

_BODY_513_OUTLOOK_NL = """Beste,

Ik geef dit door aan de heer Forrez en die zal u contacteren.

Groeten,

Toon Breyne

Van: contact@detectivebelgium.com
Verzonden: vrijdag 19 juni 2026 13:24
Aan: "toon.breyne@tbreyne.be" <toon.breyne@tbreyne.be>
Onderwerp: Offerte en informatieaanvraag

Daniel Hurchon  Privédetective
GSM -0032475331112
E-mail -contact@detectivebelgium.com
"""

_BODY_513_OUTLOOK_FR = """Bonjour,

Je transmets à M. Forrez qui vous contactera.

Cordialement,

Toon Breyne

De : contact@detectivebelgium.com
Date : mercredi, 17 juin 2026 à 11:45
À : "toon.breyne@tbreyne.be" <toon.breyne@tbreyne.be>
Objet : Re: New Message From Privédetective België - Contacteer ons

Daniel Hurchon  Privédetective
GSM -0471/31.81.20
E-mail -contact@detectivebelgium.com
"""

_BODY_CLASSIC_GT_DANIEL = """Bonjour,

Je vous ai répondu en vert sur votre mail.

Bien à vous,

Frédéric Van Houtte

> Le 16 juin 2026 à 15:23, Detective Belgique <contact@detectivebelgique.be> a écrit :
>
> MISSION : OUVRIER EN INCAPACITÉ DE TRAVAIL
> Daniel Hurchon - DetectiveBelgique.be SRL - GSM - 0471/31.81.20
"""

_BODY_NO_CITATION = """Bonjour,

Je souhaite un devis pour une filature.

Merci,
Toon Breyne
"""


def test_body_quotes_daniel_outlook_nl() -> None:
    """#513 - citation format Outlook NL + signature Daniel."""
    assert _body_quotes_daniel(_BODY_513_OUTLOOK_NL) is True


def test_body_quotes_daniel_outlook_fr() -> None:
    """#513 - citation format Outlook FR + signature Daniel."""
    assert _body_quotes_daniel(_BODY_513_OUTLOOK_FR) is True


def test_body_quotes_daniel_classic_gt() -> None:
    """#606 - citation classique avec préfixe `>`."""
    assert _body_quotes_daniel(_BODY_CLASSIC_GT_DANIEL) is True


def test_body_quotes_daniel_false_when_no_citation() -> None:
    """Nouvelle demande sans citation → False."""
    assert _body_quotes_daniel(_BODY_NO_CITATION) is False


def test_is_reply_to_daniel_wp_forwarder_with_outlook_citation() -> None:
    """#513 - forwarder WP accepté quand le body cite Daniel."""
    assert _is_reply_to_daniel(_BODY_513_OUTLOOK_NL, "wordpress@detectivebelgium.com") is True


def test_is_reply_to_daniel_wp_forwarder_without_citation() -> None:
    """Forwarder WP sans citation Daniel → pas une réponse humaine."""
    assert _is_reply_to_daniel(_BODY_NO_CITATION, "wordpress@detectivebelgium.com") is False


def test_is_reply_to_daniel_human_sender_with_classic_citation() -> None:
    """#606 - expéditeur humain + citation classique."""
    assert _is_reply_to_daniel(_BODY_CLASSIC_GT_DANIEL, "etsvanhoutte@gmail.com") is True


def test_is_reply_to_daniel_service_sender_rejected() -> None:
    """noreply + citation Daniel → rejeté."""
    assert _is_reply_to_daniel(_BODY_CLASSIC_GT_DANIEL, "noreply@example.com") is False
