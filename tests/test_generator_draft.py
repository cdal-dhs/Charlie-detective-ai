"""Tests unitaires du pipeline de génération de brouillons."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import MailboxConfig
from app.pipeline.generator import generate_draft


@pytest.mark.asyncio
async def test_generate_draft_fr_includes_original_body(monkeypatch) -> None:
    """Mail FR : le brouillon final doit contenir la proposition puis le message original du client."""

    def fake_settings() -> object:
        class FakeSettings:
            mailboxes = staticmethod(
                lambda: [
                    MailboxConfig(
                        name="test",
                        user="u@example.com",
                        app_password="secret",
                        brand="Detective Belgique FR",
                        default_lang="fr",
                        db_path=Path("data/boite1.sqlite"),
                    )
                ]
            )
            rag_top_k = 3
            cerveau2_base_url = ""
            cerveau2_api_secret = ""
            cerveau2_limit = 5
            dossier_opening_fee = 200
            report_fee = 150
            hourly_rate_day = 75
            hourly_rate_night_weekend = 95
            llm_model_default = "fake-model"
            llm_model_fallback = "fake-fallback"
            llm_model_qualifier = "fake-qualifier"

        return FakeSettings()

    monkeypatch.setattr("app.config.get_settings", fake_settings)
    monkeypatch.setattr("app.pipeline.generator.retrieve", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.pipeline.generator.query_vault", AsyncMock(return_value=([], ""))
    )
    monkeypatch.setattr(
        "app.pipeline.generator.classify_case",
        AsyncMock(return_value=("non_determine", "low", "")),
    )

    mailbox = fake_settings().mailboxes()[0]
    result = await generate_draft(
        incoming_subject="Demande de devis",
        incoming_body="Bonjour,\n\nJe souhaite un devis pour une enquête.\n\nMerci,\nPierre",
        sender="pierre@example.com",
        mailbox=mailbox,
        language="fr",
        category="demande_client",
    )

    assert "PROPOSITION DE RÉPONSE (en Français)" in result.draft
    assert "=== MESSAGE ORIGINAL DU CLIENT ===" in result.draft
    assert "Je souhaite un devis pour une enquête." in result.draft
    assert result.draft.index("PROPOSITION DE RÉPONSE") < result.draft.index("MESSAGE ORIGINAL")
