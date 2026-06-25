"""Tests du check d'objectif final (app/pipeline/objective_check.py).

v1.25.6 — Cf. #615 (Andree Marie Scurbecq) : « faire une petite enquete au bureau
de douane de Kaiserslautern » = demande SANS objectif final précis. Le brouillon
qualifiant standard sautait aux tarifs sans demander l'objectif.

Hybride : heuristique (objectif évident → clair, pas de LLM) + LLM gemma4 si
incertain + dégradation vers flou si le LLM échoue (règle d'or : faux positifs
acceptables, faux négatifs intolérables).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.objective_check import (
    _has_clear_objective_heuristic,
    assess_objective_clarity,
    extract_free_message,
)

# --- extract_free_message ---

def test_extract_free_message_strips_form_fields() -> None:
    """#615 — le message libre du client est isolé des champs formulaire."""
    body = (
        "bonjour j'aurai besoin d'un détective pour faire une petite enquete "
        "au bureau de douane de Kaiserslautern est ce que vous accepteriez de le "
        "faire ? merci\n"
        "Nom: scurbecq\nPrénom: andree marie\nTéléphone: 0484636111\n"
        "Votre profil ?: Particulier\nMentions légales: Ce formulaire..."
    )
    free = extract_free_message(body)
    assert "petite enquete" in free
    assert "scurbecq" not in free
    assert "Mentions légales" not in free
    assert "0484636111" not in free


def test_extract_free_message_no_form_fields_returns_all() -> None:
    body = "Bonjour, je souhaite prouver l'infidélité de mon conjoint."
    assert extract_free_message(body) == body.strip()


def test_extract_free_message_empty() -> None:
    assert extract_free_message("") == ""
    assert extract_free_message(None) == ""  # type: ignore[arg-type]


# --- _has_clear_objective_heuristic ---

def test_heuristic_clear_objective_filature() -> None:
    assert _has_clear_objective_heuristic(
        "Je veux faire surveiller mon mari pour prouver son infidélité."
    ) is True


def test_heuristic_clear_objective_tariff_question() -> None:
    """Une question de tarif explicite = le client sait ce qu'il veut → clair."""
    assert _has_clear_objective_heuristic("Combien coûte votre intervention ?") is True


def test_heuristic_clear_objective_recherche() -> None:
    assert _has_clear_objective_heuristic(
        "Je cherche à retrouver une personne que j'ai perdue de vue."
    ) is True


def test_heuristic_clear_objective_succession() -> None:
    """v1.25.27 — #643 : « connaître l'ampleur de la succession et réserver nos
    droits » = objectif clair (investigation patrimoniale). L'heuristique doit
    le reconnaître sans déléguer au LLM (sinon gemma répondait OBJECTIF_FLOU)."""
    assert _has_clear_objective_heuristic(
        "le père de ma femme serait mourant, ma compagne est sa seule héritière "
        "directe. Nous aimerions connaître l'ampleur de sa succession et réserver "
        "nos droits le cas échéant."
    ) is True


def test_heuristic_clear_objective_patrimoine() -> None:
    """Variantes lexicales investigation patrimoniale = objectif clair."""
    assert _has_clear_objective_heuristic(
        "Je souhaite évaluer le patrimoine de mon défunt père et faire valoir "
        "mes droits d'héritier."
    ) is True


def test_heuristic_vague_enquete_without_objective() -> None:
    """#615 — « faire une petite enquête » sans objectif final = incertain → None."""
    msg = (
        "bonjour j'aurai besoin d'un détective pour faire une petite enquete "
        "au bureau de douane de Kaiserslautern est ce que vous accepteriez de le faire ? merci"
    )
    assert _has_clear_objective_heuristic(msg) is None


def test_heuristic_empty_is_vague() -> None:
    assert _has_clear_objective_heuristic("") is False


def test_heuristic_lapidary_is_vague() -> None:
    """Message très court (< 60 chars) sans objectif → flou."""
    assert _has_clear_objective_heuristic("Bonjour, besoin d'aide.") is False


# --- assess_objective_clarity : LLM branché ---

@pytest.mark.asyncio
async def test_assess_objective_clear_skips_llm() -> None:
    """Objectif évident (heuristique) → pas d'appel LLM."""
    with patch("app.pipeline.objective_check.complete", new=AsyncMock()) as m:
        verdict = await assess_objective_clarity(
            "Je veux prouver l'infidélité de mon mari par filature."
        )
    assert verdict is True
    m.assert_not_called()


@pytest.mark.asyncio
async def test_assess_objective_succession_skips_llm() -> None:
    """#643 — l'objectif succession est reconnu clair par heuristique, sans LLM."""
    with patch("app.pipeline.objective_check.complete", new=AsyncMock()) as m:
        verdict = await assess_objective_clarity(
            "Nous aimerions connaître l'ampleur de sa succession et réserver "
            "nos droits le cas échéant."
        )
    assert verdict is True
    m.assert_not_called()


@pytest.mark.asyncio
async def test_assess_objective_vague_llm_says_flou() -> None:
    """#615 — heuristique incertaine → LLM dit OBJECTIF_FLOU → flou."""

    async def fake_complete(model, messages, max_tokens=20, temperature=0.1):
        return "OBJECTIF_FLOU"

    with patch("app.pipeline.objective_check.complete", new=AsyncMock(side_effect=fake_complete)):
        verdict = await assess_objective_clarity(
            "j'aurai besoin d'un détective pour faire une petite enquete au bureau "
            "de douane de Kaiserslautern est ce que vous accepteriez de le faire ?"
        )
    assert verdict is False


@pytest.mark.asyncio
async def test_assess_objective_clear_llm_says_clair() -> None:
    """Heuristique incertaine mais LLM juge l'objectif clair → clair."""

    async def fake_complete(model, messages, max_tokens=20, temperature=0.1):
        return "OBJECTIF_CLAIR"

    with patch("app.pipeline.objective_check.complete", new=AsyncMock(side_effect=fake_complete)):
        verdict = await assess_objective_clarity(
            "Je voudrais que vous constatiez l'état du terrain avant achat du bien."
        )
    assert verdict is True


@pytest.mark.asyncio
async def test_assess_objective_llm_failure_degrades_to_flou() -> None:
    """LLM raise → dégradation vers flou (règle d'or : faux positif acceptable)."""

    async def fake_complete(model, messages, max_tokens=20, temperature=0.1):
        raise RuntimeError("Ollama 503")

    with patch("app.pipeline.objective_check.complete", new=AsyncMock(side_effect=fake_complete)):
        verdict = await assess_objective_clarity(
            "j'aurai besoin d'un détective pour une enquete au bureau de douane"
        )
    assert verdict is False


@pytest.mark.asyncio
async def test_assess_objective_empty_is_flou_no_llm() -> None:
    with patch("app.pipeline.objective_check.complete", new=AsyncMock()) as m:
        assert await assess_objective_clarity("") is False
    m.assert_not_called()
