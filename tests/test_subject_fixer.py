"""Tests du correcteur de sujets illisibles (app/pipeline/subject_fixer.py).

v1.25.3 — corrige les sujets truffés d'homoglyphes (ex: #614 itsme cyrillique)
via LLM. Dégradation silencieuse : si le LLM échoue ou ne propose rien de
mieux, on conserve l'original (jamais de crash).
"""

from __future__ import annotations

# Les cyrilliques dans les strings de test sont intentionnels : on teste la
# détection d'homoglyphes (ex: #614 itsme). RUF001 = ambiguous unicode char.
# ruff: noqa: RUF001, RUF003
import pytest

from app.pipeline.subject_fixer import (
    _clean,
    fix_subject_llm,
    is_subject_suspect,
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
