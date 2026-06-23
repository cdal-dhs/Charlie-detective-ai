"""Tests is_newsletter / quick_classify (app/pipeline/prefilter.py).

v1.25.5 — détection newsletter durcie pour rattraper #619 (Arval, marketing
B2B via Eloqua) classé demande_client à tort :
  - matching accent-insensible (découvrez == decouvrez)
  - signatures URL plateformes marketing (Eloqua elqTrackId/elqaid, Mailchimp mc_cid/mc_eid)
  - sous-domaines marketing (info./news./newsletter./email./marketing./...)
"""

from __future__ import annotations

from email.message import EmailMessage

from app.pipeline.prefilter import is_newsletter, quick_classify


def _msg(subject: str, sender: str, body: str) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m.set_content(body)
    return m


# --- #619 : newsletter Arval via Eloqua, classée demande_client à tort ---


def test_619_arval_newsletter_detected() -> None:
    """#619 — Arval marketing B2B : sous-domaine info. + URL Eloqua + « Découvrez »."""
    body = (
        "Monsieur Daniel Hurchon,\n\n"
        "Découvrez nos conseils pour préparer vos vacances d'été.\n"
        "https://s.arval.com/e?elqTrackId=ABC123&elqaid=456\n"
    )
    assert is_newsletter(_msg(
        "Arval | Quelques conseils pour préparer vos vacances d'été",
        "arval@info.arval.com",
        body,
    )) is True


def test_619_quick_classify_returns_newsletter() -> None:
    """Le pré-filtre rapide court-circuite le LLM → newsletter pour #619."""
    body = "Découvrez nos conseils https://s.arval.com/e?elqTrackId=ABC&elqaid=456"
    m = _msg("Arval | Conseils vacances", "arval@info.arval.com", body)
    assert quick_classify(m) == "newsletter"


# --- Accent-insensibilité ---


def test_accent_insensitive_keyword_in_subject() -> None:
    """« Découvrez » (accentué) matche le keyword « decouvrez » (sans accent)."""
    assert is_newsletter(_msg("Découvrez nos offres exclusives", "x@y.com", "body")) is True


def test_accent_insensitive_keyword_in_body() -> None:
    m = _msg("Sujet quelconque", "x@y.com", "Gérez vos préférences de communication ici")
    assert is_newsletter(m) is True


# --- Détection URL Eloqua / Mailchimp seule (sender neutre) ---


def test_eloqua_url_detected_neutral_sender() -> None:
    body = "Bonjour, voir notre offre https://go.oracle.com/e?elqTrackId=1&elqaid=2"
    assert is_newsletter(_msg("Mise à jour produit", "comm@oracle.com", body)) is True


def test_mailchimp_url_detected() -> None:
    body = "Voir https://mailchimp.com/c?mc_cid=news&mc_eid=42"
    assert is_newsletter(_msg("Bulletin mensuel", "news@brand.com", body)) is True


# --- Sous-domaines marketing ---


def test_news_subdomain_detected() -> None:
    assert is_newsletter(_msg("Bienvenue", "noreply@news.brand.com", "body")) is True


def test_email_subdomain_detected() -> None:
    assert is_newsletter(_msg("Promo", "promo@email.brand.com", "body")) is True


def test_marketing_subdomain_detected() -> None:
    assert is_newsletter(_msg("Offre", "comms@marketing.brand.com", "body")) is True


# --- Non-régressions : vraie demande client, sender légitime ---


def test_real_demande_client_not_newsletter() -> None:
    """Une vraie demande client directe ne doit pas être taguée newsletter."""
    body = (
        "Bonjour, je souhaite une filature. Pouvez-vous me rappeler au 0488411192 ? "
        "Merci, Serge"
    )
    assert is_newsletter(_msg(
        "Demande de suivi de dossier",
        "yashwantsharma@colorsofindiatours.com",
        body,
    )) is False


def test_info_in_local_part_not_subdomain() -> None:
    """info@detectivebelgium.com : « info » dans la partie locale, pas le domaine → False."""
    assert is_newsletter(_msg("Demande de devis", "info@detectivebelgium.com", "body")) is False


def test_contact_brand_not_marketing_subdomain() -> None:
    """contact@brand.com : domaine = brand.com, ne commence pas par un sous-domaine marketing."""
    assert is_newsletter(_msg("Question", "contact@brand.com", "body")) is False


def test_plain_subject_no_markers_not_newsletter() -> None:
    m = _msg("Votre rendez-vous", "nathalie@example.com", "Confirmez le rdv.")
    assert is_newsletter(m) is False
