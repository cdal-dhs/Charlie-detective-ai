import pytest

from app.pipeline.case_classifier import _extract_case_type_from_json, classify_case


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"case_type": "infidelite_filature", "confidence": "high", "reason": "x"}', "infidelite_filature"),
        ("```json\n{\"case_type\": \"recherche_personne\", \"confidence\": \"medium\", \"reason\": \"x\"}\n```", "recherche_personne"),
        ('cas de filature et surveillance de mon conjoint', "infidelite_filature"),
        ('recherche adresse de mon frère disparu', "recherche_personne"),
        ('piratage de mon compte bancaire', "non_determine"),
    ],
)
def test_extract_case_type(raw, expected):
    case, conf, reason = _extract_case_type_from_json(raw)
    assert case == expected


@pytest.mark.asyncio
async def test_classify_case_returns_valid_tuple(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return '{"case_type": "incapacite_travail", "confidence": "high", "reason": "mail parle d\'arrêt maladie"}'

    monkeypatch.setattr("app.pipeline.case_classifier.complete", fake_complete)
    case, confidence, reason = await classify_case("mon ouvrier est en arrêt maladie", "il travaille au noir")
    assert case == "incapacite_travail"
    assert confidence == "high"
    assert "arrêt maladie" in reason


@pytest.mark.asyncio
async def test_classify_case_fallback_on_error(monkeypatch):
    async def fake_complete(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.pipeline.case_classifier.complete", fake_complete)
    case, confidence, reason = await classify_case("x", "y")
    assert case == "non_determine"
    assert confidence == "low"
