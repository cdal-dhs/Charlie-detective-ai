"""Tests v1.25.12 — détection formulaire WordPress dans le pré-filtre.

Bug : les formulaires WP envoyés par mail@/wordpress@/contact@detective*
peuvent être classés newsletter/autre/rappel/facture à tort par le pré-filtre
rapide (sujets trompeurs, keywords "mentions légales", "devis", "facturation").
Tolérance zéro : un formulaire WP structuré doit TOUJOURS être demande_client.
"""

from email.message import EmailMessage

import pytest

from app.pipeline.prefilter import _is_wp_contact_form, is_wordpress_contact_form, quick_classify


_BODY_515_NL = """Achternaam: Hairemans
Voornaam: Nathalie
Telefoonnummer: 0468287587
Tijdsstippen: 15u
Uw profiel ?: Particulier
Hoe kunnen wij u helpen ? Vertel ons meer: Beste, dit is een vraag.
Wettelijke kennisgeving & privacybeleid: Dit formulier stelt ons in staat...
"""

_BODY_520_FR = """J'ai un ex mari qui dit ne pas boire j'aimerais trouver des preuves pour pouvoir demander la garde exclusive de mes enfants.
Nom: Marx
Prénom: Aurore
Téléphone: 0467073017
Heure de contact: 17h40
Votre profil ?: Particulier
Mentions légales & Politique de Confidentialité: Ce formulaire nous permet de collecter...
"""

_BODY_600_FR = """Objet : Demande de devis – Enquête de solvabilité et localisation (Créance > 100 K€)

Je sollicite vos services pour mener une enquête approfondie visant à localiser le débiteur.

Nom: Sardi
Prénom: Laurent
Téléphone: 0683168158
Heure de contact: des 8h00
Votre profil ?: Particulier
Mentions légales & Politique de Confidentialité: Ce formulaire nous permet...
"""

_BODY_511_WP_LOGIN = """Identifiant : detective
Pour configurer votre mot de passe, rendez-vous à l’adresse suivante :
https://www.detectivebelgique.be/monodetective?login=detective&key=xxx&action=resetpass
"""


def _msg(subject: str, body: str, sender: str = "mail@detectivebelgique.be") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.set_content(body)
    return msg


# ── _is_wp_contact_form (shared logic) ───────────────────────────


def test_is_wp_contact_form_515_nl():
    assert _is_wp_contact_form(_BODY_515_NL) is True


def test_is_wp_contact_form_520_fr():
    assert _is_wp_contact_form(_BODY_520_FR) is True


def test_is_wp_contact_form_600_fr():
    assert _is_wp_contact_form(_BODY_600_FR) is True


def test_is_wp_contact_form_negative_login_email():
    """Notification de connexion WP sans champs contact = PAS un formulaire client."""
    assert _is_wp_contact_form(_BODY_511_WP_LOGIN) is False


def test_is_wp_contact_form_negative_plain_text():
    """Mail texte libre sans champs structurés = PAS un formulaire WP."""
    assert _is_wp_contact_form("Bonjour, j'aimerais un devis pour une filature. Merci.") is False


# ── is_wordpress_contact_form (Message wrapper) ─────────────────


def test_is_wordpress_contact_form_message_520():
    msg = _msg("Nouveau Message De Détective privé Belgique - Prenons contact", _BODY_520_FR)
    assert is_wordpress_contact_form(msg) is True


def test_is_wordpress_contact_form_message_login_false():
    msg = _msg("[Détective privé Belgique] Détails de connexion", _BODY_511_WP_LOGIN)
    assert is_wordpress_contact_form(msg) is False


# ── quick_classify : formulaire WP prioritaire ───────────────────


def test_quick_classify_wp_form_before_newsletter():
    """#520 : sujet contient 'Nouveau Message' (newsletter-like) mais champs WP → demande_client."""
    msg = _msg("Nouveau Message De Détective privé Belgique - Prenons contact", _BODY_520_FR)
    assert quick_classify(msg) == "demande_client"


def test_quick_classify_wp_form_before_service():
    """#600 : body contient 'devis', 'créance', 'facturation' mais champs WP → demande_client."""
    msg = _msg("Nouveau Message De Détective privé Belgique - Prenons contact", _BODY_600_FR)
    assert quick_classify(msg) == "demande_client"


def test_quick_classify_wp_form_trompeur_reset_password():
    """#515 : sujet WP trompeur 'Réinitialisation du mot de passe' mais champs NL → demande_client."""
    msg = _msg(
        "[Privédetective België] Réinitialisation du mot de passe",
        _BODY_515_NL,
        sender="wordpress@detectivebelgium.com",
    )
    assert quick_classify(msg) == "demande_client"


def test_quick_classify_wp_login_stays_autre():
    """Email de connexion WP sans champs contact → reste autre/service."""
    msg = _msg(
        "[Détective privé Belgique] Détails de connexion",
        _BODY_511_WP_LOGIN,
        sender="wordpress@detectivebelgique.be",
    )
    # Pas de champs WP → le pré-filtre suit les règles normales.
    category = quick_classify(msg)
    assert category in {"autre", "rappel", None}


@pytest.mark.parametrize(
    "sender",
    [
        "mail@detectivebelgique.be",
        "wordpress@detectivebelgium.com",
        "contact@detectivebelgique.be",
        "mail@detectivebelgium.com",
    ],
)
def test_quick_classify_wp_form_any_forwarder(sender: str):
    """Peu importe le forwarder technique, un formulaire WP structuré = demande_client."""
    msg = _msg("Nouveau Message", _BODY_520_FR, sender=sender)
    assert quick_classify(msg) == "demande_client"
