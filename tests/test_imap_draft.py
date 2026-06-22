"""Tests unitaires pour le dépôt IMAP des brouillons."""

from __future__ import annotations

from app.delivery.imap_draft import _build_draft_body
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult


def test_build_draft_body_does_not_duplicate_original_body() -> None:
    """Le message original est déjà dans gen.draft, on ne le répète pas."""
    incoming = IncomingMail(
        sender="client@example.com",
        subject="Demande",
        body="Bonjour, je souhaite un devis.",
        received_at="Mon, 18 Jun 2026 10:00:00 +0000",
        message_id="<abc@example.com>",
    )
    gen = GenerationResult(
        draft="✉️ PROPOSITION\nBonjour, voici la proposition.\n\n=== MESSAGE ORIGINAL DU CLIENT ===\nBonjour, je souhaite un devis.",
        raw_draft="Bonjour, voici la proposition.",
        language="fr",
        rag_pairs=[],
        model_used="test",
        category="demande_client",
    )
    body = _build_draft_body(incoming, gen, mail_id=123, base_url="https://example.com")
    assert "⚠️  BROUILLON IA" in body
    assert "Dossier cockpit : https://example.com/app/conversation/123" in body
    assert body.count("MESSAGE ORIGINAL DU CLIENT") == 1
    assert body.count("Bonjour, je souhaite un devis.") == 1
