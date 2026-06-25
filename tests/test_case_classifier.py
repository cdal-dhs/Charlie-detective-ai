import pytest

from app.pipeline.case_classifier import _extract_case_type_from_json, classify_case


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            '{"case_type": "infidelite_filature", "confidence": "high", "reason": "x"}',
            "infidelite_filature",
        ),
        (
            "```json\n"
            '{"case_type": "recherche_personne", "confidence": "medium", "reason": "x"}\n'
            "```",
            "recherche_personne",
        ),
        (
            '{"case_type": "unknown_case", "confidence": "high", "reason": "x"}',
            "non_determine",
        ),
    ],
)
def test_extract_case_type(raw, expected):
    case, _conf, _reason = _extract_case_type_from_json(raw)
    assert case == expected


def test_extract_case_type_fallback_keyword():
    # Le fallback doit analyser le search_text (mail original), pas la réponse LLM.
    case, _conf, _reason = _extract_case_type_from_json(
        "pas du json valide",
        search_text=(
            "mon conjoint me trompe, j'ai besoin d'une filature et surveillance"
        ),
    )
    assert case == "infidelite_filature"


def test_extract_case_type_fallback_work_context():
    # "travail" dans "lieu de travail" ne doit PAS déclencher incapacite_travail.
    case, _conf, _reason = _extract_case_type_from_json(
        "pas du json",
        search_text=(
            "filature de mon collaborateur à la sortie de son lieu de travail"
        ),
    )
    assert case == "infidelite_filature"


def test_extract_case_type_fallback_succession():
    """v1.25.27 — #643 : mots-clés succession/héritage/patrimoine →
    investigation_successorale (et non non_determine)."""
    case, _conf, _reason = _extract_case_type_from_json(
        "pas du json",
        search_text=(
            "ma compagne est la seule héritière de son père, nous voulons "
            "connaître l'ampleur de la succession et réserver nos droits"
        ),
    )
    assert case == "investigation_successorale"


@pytest.mark.asyncio
async def test_classify_case_returns_valid_tuple(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return (
            '{"case_type": "incapacite_travail", '
            '"confidence": "high", '
            '"reason": "mail parle d\'arrêt maladie"}'
        )

    monkeypatch.setattr("app.pipeline.case_classifier.complete", fake_complete)
    case, confidence, reason = await classify_case(
        "mon ouvrier est en arrêt maladie", "il travaille au noir"
    )
    assert case == "incapacite_travail"
    assert confidence == "high"
    assert "arrêt maladie" in reason


@pytest.mark.asyncio
async def test_classify_case_fallback_on_error(monkeypatch):
    async def fake_complete(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.pipeline.case_classifier.complete", fake_complete)
    case, confidence, _reason = await classify_case("x", "y")
    assert case == "non_determine"
    assert confidence == "low"
