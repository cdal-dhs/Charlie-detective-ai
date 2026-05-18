from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cerveau_client import VaultNote
from app.charlie import _is_vault_relevant, ask_charlie


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
