"""Tests spécifiques pour le cas de figure récupération de dette."""

from __future__ import annotations

from app.pipeline.case_classifier import _extract_case_type_from_json


def test_fallback_detecte_dette() -> None:
    case, _conf, _reason = _extract_case_type_from_json(
        "pas du json",
        search_text="Enquête sur un membre de mon entourage qui me doit une grosse somme d'argent",
    )
    assert case == "recuperation_dette"


def test_fallback_dette_reconnaissance() -> None:
    case, _conf, _reason = _extract_case_type_from_json(
        "pas du json",
        search_text="Je cherche à récupérer une créance avec reconnaissance de dette",
    )
    assert case == "recuperation_dette"
