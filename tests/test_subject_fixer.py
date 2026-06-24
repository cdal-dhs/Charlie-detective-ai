"""Tests du correcteur de sujets illisibles/incohérents (app/pipeline/subject_fixer.py).

v1.25.4 — corrige les sujets truffés d'homoglyphes (ex: #614 itsme cyrillique)
ET les sujets non-représentatifs (ex: #515 forwarder WP « Réinitialisation du
mot de passe ») via LLM. Tag [NO_EMAIL_IN_THE_FORM] pour les forwarders WP.
Dégradation silencieuse : si le LLM échoue ou ne propose rien de mieux,
on conserve l'original (jamais de crash).
"""

from __future__ import annotations

# Les cyrilliques dans les strings de test sont intentionnels : on teste la
# détection d'homoglyphes (ex: #614 itsme). RUF001 = ambiguous unicode char.
# ruff: noqa: RUF001, RUF003
import pytest

from app.pipeline.subject_fixer import (
    _clean,
    fix_subject_llm,
    has_client_email_in_body,
    is_subject_suspect,
    is_wp_forwarder,
    mask_forwarder_sender,
    tag_no_email,
)

# --- is_subject_suspect ---


def test_suspect_cyrillic_itsme() -> None:
    """#614 — homoglyphes cyrilliques + chiffre romain ⅿ = suspect."""
    subject = "іtѕⅿе-Bеvеіlіngѕmеldіng"
    assert is_subject_suspect(subject) is True


def test_suspect_pure_cyrillic() -> None:
    assert is_subject_suspect("Привет мир") is True


def test_suspect_greek() -> None:
    assert is_subject_suspect("Ора ναι") is True  # О et ν grecs


def test_not_suspect_ascii_plain() -> None:
    assert is_subject_suspect("Demande de suivi de dossier") is False


def test_not_suspect_french_accents() -> None:
    """Les accents Latin (é è à ç) ne sont PAS des confusables."""
    assert is_subject_suspect("Réclamation — début d'enquête") is False


def test_not_suspect_empty() -> None:
    assert is_subject_suspect("") is False
    assert is_subject_suspect(None) is False  # type: ignore[arg-type]


def test_not_suspect_dutch_accents() -> None:
    assert is_subject_suspect("Bevestiging van uw aanvraag") is False


# --- _clean ---


def test_clean_strips_quotes_and_prefix() -> None:
    assert _clean('"itsme-Bevelingsmelding"') == "itsme-Bevelingsmelding"
    assert _clean("Sujet : itsme-Bevelingsmelding") == "itsme-Bevelingsmelding"
    assert _clean("Subject: Demand de suivi") == "Demand de suivi"


def test_clean_keeps_first_line_only() -> None:
    raw = "itsme-Bevelingsmelding\n\nJustification: homoglyphes détectés"
    assert _clean(raw) == "itsme-Bevelingsmelding"


# --- fix_subject_llm (LLM mocké) ---


@pytest.mark.asyncio
async def test_fix_subject_returns_clean(monkeypatch) -> None:
    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return "itsme-Bevelingsmelding"

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе-Bеvеіlіngѕmеldіng", "body hint")
    assert fixed == "itsme-Bevelingsmelding"


@pytest.mark.asyncio
async def test_fix_subject_strips_quotes_from_llm(monkeypatch) -> None:
    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return '"itsme-Bevelingsmelding"'

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе", "body")
    assert fixed == "itsme-Bevelingsmelding"


@pytest.mark.asyncio
async def test_fix_subject_no_improvement_returns_none(monkeypatch) -> None:
    """Si le LLM renvoie le même sujet (déjà lisible), on garde l'original → None."""

    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return "Demande de suivi"

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("Demande de suivi", "body")
    assert fixed is None


@pytest.mark.asyncio
async def test_fix_subject_llm_failure_returns_none(monkeypatch) -> None:
    """LLM raise → dégradation silencieuse (None), pas de crash."""

    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        raise RuntimeError("Ollama 503")

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе", "body")
    assert fixed is None


@pytest.mark.asyncio
async def test_fix_subject_empty_llm_returns_none(monkeypatch) -> None:
    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return ""

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе", "body")
    assert fixed is None


@pytest.mark.asyncio
async def test_fix_subject_too_long_returns_none(monkeypatch) -> None:
    """Hallucination LLM (sujet > 200 chars) rejetée → None."""

    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return "x" * 250

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе", "body")
    assert fixed is None


@pytest.mark.asyncio
async def test_fix_subject_empty_input_returns_none() -> None:
    assert await fix_subject_llm("", "body") is None


@pytest.mark.asyncio
async def test_fix_subject_preserves_french_accents(monkeypatch) -> None:
    """Le sujet corrigé peut contenir des accents FR/NL (ASCII non requis strict)."""

    async def fake_complete(model, messages, max_tokens=120, temperature=0.2):
        return "Demande — suivi d'enquête"

    monkeypatch.setattr("app.pipeline.subject_fixer.complete", fake_complete)
    fixed = await fix_subject_llm("іtѕⅿе", "body")
    assert fixed == "Demande — suivi d'enquête"


# --- is_wp_forwarder / tag_no_email ---


def test_wp_forwarder_matches_wordpress_sender() -> None:
    assert is_wp_forwarder("wordpress@detectivebelgium.com") is True
    assert is_wp_forwarder("mail@detectivebelgique.be") is True
    assert is_wp_forwarder("contact@detectivebelgium.com") is True


def test_wp_forwarder_case_insensitive() -> None:
    assert is_wp_forwarder("WordPress@DetectiveBelgium.com") is True


def test_wp_forwarder_rejects_normal_senders() -> None:
    """#614 = client direct, pas un forwarder WP."""
    assert is_wp_forwarder("yashwantsharma@colorsofindiatours.com") is False
    assert is_wp_forwarder("client@gmail.com") is False
    # L'agent Resend (digitalhs) ne doit PAS matcher.
    assert is_wp_forwarder("agent@digitalhs.biz") is False
    assert is_wp_forwarder("") is False
    assert is_wp_forwarder(None) is False  # type: ignore[arg-type]


def test_tag_no_email_adds_tag_for_wp_forwarder() -> None:
    """#515 — forwarder WP → suffixe [NO_EMAIL_IN_THE_FORM]."""
    out = tag_no_email(
        "[Privédetective België] Réinitialisation du mot de passe", "wordpress@detectivebelgium.com"
    )
    assert out.endswith("[NO_EMAIL_IN_THE_FORM]")
    assert "[NO_EMAIL_IN_THE_FORM]" in out


def test_tag_no_email_idempotent() -> None:
    """Ne re-tag pas si déjà présent."""
    s = "Demande [NO_EMAIL_IN_THE_FORM]"
    assert tag_no_email(s, "wordpress@detectivebelgium.com") == s


def test_tag_no_email_no_change_for_normal_sender() -> None:
    """Sender normal (#614) → sujet inchangé."""
    s = "itsme-Beveilingsmelding: uw dienst stopgezet"
    assert tag_no_email(s, "yashwantsharma@colorsofindiatours.com") == s


def test_tag_no_email_empty_subject() -> None:
    out = tag_no_email("", "wordpress@detectivebelgium.com")
    assert out == "[NO_EMAIL_IN_THE_FORM]"


def test_tag_no_email_no_tag_when_client_email_in_body() -> None:
    """Si le body contient un vrai email client, on ne tagge pas le sujet."""
    body = "Voornaam: John\nTelefoonnummer: 0477/123456\nEmail: john@client.be"
    out = tag_no_email(
        "[Privédetective België] Nieuwe aanvraag", "wordpress@detectivebelgium.com", body
    )
    assert "[NO_EMAIL_IN_THE_FORM]" not in out


def test_has_client_email_detects_real_email() -> None:
    body = "Contact : client@example.com"
    assert has_client_email_in_body(body) is True


def test_has_client_email_ignores_own_domains_and_no_reply() -> None:
    body = "From: wordpress@detectivebelgium.com\nnoreply@service.com"
    assert has_client_email_in_body(body) is False


def test_mask_forwarder_sender_masks_without_client_email() -> None:
    body = "Achternaam: Dupont\nTelefoonnummer: 0477/123456"
    assert (
        mask_forwarder_sender("wordpress@detectivebelgium.com", body)
        == "NO_EMAIL_IN_THE_FORM"
    )


def test_mask_forwarder_sender_keeps_real_sender_when_client_email_present() -> None:
    """v1.25.26 — un email dans le body n'est pas un signal fiable : un
    forwarder sans Reply-To reste NO_EMAIL_IN_THE_FORM (règle CDAL stricte)."""
    body = "Email: client@example.com\nTelefoonnummer: 0477/123456"
    assert (
        mask_forwarder_sender("wordpress@detectivebelgium.com", body)
        == "NO_EMAIL_IN_THE_FORM"
    )


def test_mask_forwarder_sender_no_change_for_normal_sender() -> None:
    assert mask_forwarder_sender("client@gmail.com", "") == "client@gmail.com"
