"""Tests unitaires pour le rendu multilingue des brouillons."""

from __future__ import annotations

from app.pipeline.draft_renderer import render_draft_with_translations


def test_render_fr_includes_proposition_then_original() -> None:
    """Mail FR : proposition FR puis message original du client en dessous."""
    draft = render_draft_with_translations(
        incoming_body="Bonjour,\n\nJe souhaite un devis pour une filature.",
        draft_fr="Bonjour,\n\nMerci pour votre demande. Voici les informations nécessaires.",
        source_lang="fr",
        incoming_subject="Demande de devis",
    )
    assert "✉️ PROPOSITION DE RÉPONSE (en Français)" in draft
    assert "=== MESSAGE ORIGINAL DU CLIENT ===" in draft
    assert "Je souhaite un devis" in draft
    assert draft.index("PROPOSITION DE RÉPONSE") < draft.index("MESSAGE ORIGINAL")


def test_render_nl_structure_four_blocks_plus_original() -> None:
    """Mail NL : 4 blocs multilingues + message original en dessous."""
    draft = render_draft_with_translations(
        incoming_body="Beste, ik wil een offerte voor surveillance.",
        draft_fr="Bonjour,\n\nMerci pour votre demande.",
        source_lang="nl",
        incoming_subject="Offerte aanvraag",
        translation_to_fr="Bonjour, je souhaite un devis pour une filature.",
        translation_from_fr="Beste, dank u voor uw aanvraag.",
    )
    assert "📩 EMAIL D'ORIGINE (Néerlandais)" in draft
    assert "🇫🇷 TRADUCTION FR (pour lecture Daniel)" in draft
    assert "✉️ PROPOSITION DE RÉPONSE (en Français)" in draft
    assert "🌍 TRADUCTION DE LA PROPOSITION (Néerlandais" in draft
    assert "=== MESSAGE ORIGINAL DU CLIENT ===" in draft
    assert draft.index("PROPOSITION DE RÉPONSE") < draft.index("MESSAGE ORIGINAL")


def test_render_fallback_when_translations_missing() -> None:
    """Traductions indisponibles : on garde la proposition FR + message original."""
    draft = render_draft_with_translations(
        incoming_body="Hello, I need a quote.",
        draft_fr="Bonjour,\n\nMerci pour votre demande.",
        source_lang="en",
        incoming_subject="Quote request",
        translation_to_fr="",
        translation_from_fr="",
    )
    assert "⚠️ Mail entrant en Anglais (traductions indisponibles)" in draft
    assert "Bonjour," in draft
    assert "=== MESSAGE ORIGINAL DU CLIENT ===" in draft
    assert "Hello, I need a quote." in draft
