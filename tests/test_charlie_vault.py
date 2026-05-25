from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cerveau_client import VaultNote
from app.charlie import _extract_dossier_id, _extract_entreprise_name, _extract_entreprise_info, _is_vault_relevant, ask_charlie


def test_extract_dossier_id_from_dossier_colon():
    assert _extract_dossier_id("donne moi ce que tu sais sur le dossier : ADF") == "ADF"


def test_extract_dossier_id_from_affaire():
    assert _extract_dossier_id("l'affaire XYZ123") == "XYZ123"


def test_extract_dossier_id_from_hash():
    assert _extract_dossier_id("Que sais-tu sur #PROJ42 ?") == "PROJ42"


def test_extract_dossier_id_none():
    assert _extract_dossier_id("Combien de mails aujourd'hui ?") is None


def test_is_vault_relevant_no_sql():
    assert _is_vault_relevant("Bonjour", "") is True


def test_is_vault_relevant_with_sql_no_keyword():
    assert _is_vault_relevant(
        "Combien de mails aujourd'hui?", "SELECT * FROM mail_processed"
    ) is False


def test_is_vault_relevant_with_sql_and_keyword():
    assert _is_vault_relevant(
        "Quels dossiers similaires?", "SELECT * FROM mail_processed"
    ) is True


@pytest.mark.asyncio
async def test_ask_charlie_calls_vault_when_no_sql():
    mock_settings = MagicMock()
    mock_settings.llm_model_default = "test-model"
    mock_settings.cerveau2_base_url = "http://test"
    mock_settings.cerveau2_api_secret = "secret"
    mock_settings.cerveau2_limit = 3

    with (
        patch("app.charlie.get_settings", return_value=mock_settings),
        patch(
            "app.charlie.complete",
            new=AsyncMock(return_value="SQL:\n---\nRÉPONSE: Bonjour !"),
        ),
        patch(
            "app.charlie.query_vault",
            new=AsyncMock(return_value=[VaultNote(path="notes/test.md", content="Contenu test")]),
        ),
    ):
        result = await ask_charlie("Bonjour", db_path=Path("/dev/null"))

    assert len(result.vault_notes) == 1
    assert result.vault_notes[0].path == "notes/test.md"


@pytest.mark.asyncio
async def test_ask_charlie_no_vault_for_pure_sql():
    mock_settings = MagicMock()
    mock_settings.llm_model_default = "test-model"
    mock_settings.cerveau2_base_url = "http://test"
    mock_settings.cerveau2_api_secret = "secret"
    mock_settings.cerveau2_limit = 3

    with (
        patch("app.charlie.get_settings", return_value=mock_settings),
        patch(
            "app.charlie.complete",
            new=AsyncMock(
                return_value="SQL: SELECT id FROM mail_processed\n---\nRÉPONSE: Voici les résultats"
            ),
        ),
        patch("app.charlie.run_sql", new=AsyncMock(return_value=[{"id": 1}])),
        patch(
            "app.charlie.query_vault", new=AsyncMock(return_value=[])
        ) as mock_vault,
    ):
        result = await ask_charlie("Combien de mails aujourd'hui?", db_path=Path("/dev/null"))

    assert result.vault_notes == []
    mock_vault.assert_not_called()


@pytest.mark.asyncio
async def test_ask_charlie_passes_dossier_id_to_vault():
    mock_settings = MagicMock()
    mock_settings.llm_model_default = "test-model"
    mock_settings.cerveau2_base_url = "http://test"
    mock_settings.cerveau2_api_secret = "secret"
    mock_settings.cerveau2_limit = 3

    with (
        patch("app.charlie.get_settings", return_value=mock_settings),
        patch(
            "app.charlie.complete",
            new=AsyncMock(
                return_value="SQL: SELECT id FROM mail_processed\n---\nRÉPONSE: Voici"
            ),
        ),
        patch("app.charlie.run_sql", new=AsyncMock(return_value=[{"id": 1}])),
        patch(
            "app.charlie.query_vault", new=AsyncMock(return_value=[])
        ) as mock_vault,
    ):
        result = await ask_charlie(
            "donne moi ce que tu sais sur le dossier : ADF",
            db_path=Path("/dev/null"),
        )

    assert result.vault_notes == []
    mock_vault.assert_awaited_once()
    call_kwargs = mock_vault.await_args.kwargs
    assert call_kwargs["dossier_id"] == "ADF"


# ── Tests _extract_entreprise_name ──

def test_extract_entreprise_name_with_accent():
    assert _extract_entreprise_name("où se trouve le siège de ADF Group ?") == "ADF Group"


def test_extract_entreprise_name_without_accent():
    assert _extract_entreprise_name("ou se trouve le siege de ADF Group ?") == "ADF Group"


def test_extract_entreprise_name_acronym():
    assert _extract_entreprise_name("Quelle est l'adresse de BPost ?") == "BPost"


def test_extract_entreprise_name_no_keyword():
    assert _extract_entreprise_name("Combien de mails aujourd'hui ?") is None


# ── Tests _extract_entreprise_info ──

def test_extract_entreprise_info_yaml_frontmatter():
    notes = [
        VaultNote(path="fiches/adf.md", content="---\nnom: ADF Group\nsiege: Bruxelles\n---\n# ADF\n")
    ]
    result = _extract_entreprise_info(notes, "ADF Group")
    assert result is not None
    assert "Bruxelles" in result


def test_extract_entreprise_info_markdown_inline():
    notes = [
        VaultNote(path="fiches/adf.md", content="# ADF Group\n**Siège** : Waterloo\n**Adresse** : Chaussée Bara 213")
    ]
    result = _extract_entreprise_info(notes, "ADF Group")
    assert result is not None
    assert "Waterloo" in result


def test_extract_entreprise_info_ignores_daniel_signature():
    notes = [
        VaultNote(
            path="emails/adf.md",
            content="Correspondance ADF Group\nSiège Social : Chaussée Bara 213, 1410 Waterloo\nDétectiveBelgique — Daniel Hurchon — 0779.433.503",
        )
    ]
    result = _extract_entreprise_info(notes, "ADF Group")
    assert result is None  # La signature de Daniel doit être ignorée


def test_extract_entreprise_info_fallback_emails():
    notes = [
        VaultNote(
            path="emails/adf.md",
            content="Mail de contact ADF Group\nDe : john.doe@groupeadf.com\nTél : +32 2 123 45 67\nM. John Doe",
        )
    ]
    result = _extract_entreprise_info(notes, "ADF Group")
    assert result is not None
    assert "groupeadf.com" in result
    assert "john.doe" in result


def test_extract_entreprise_info_no_match():
    notes = [VaultNote(path="fiches/xyz.md", content="# XYZ\nSiège : Namur")]
    result = _extract_entreprise_info(notes, "ADF Group")
    assert result is None
