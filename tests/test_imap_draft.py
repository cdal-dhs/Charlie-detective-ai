"""Tests unitaires pour le dépôt IMAP des brouillons."""

from __future__ import annotations

from app.delivery.imap_draft import _build_draft_body
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult


def test_build_draft_body_does_not_duplicate_original_body() -> None:
    """Le message original est déjà dans gen.draft, on ne le répète pas.
    Mais l'EMAIL #id et l'adresse du client doivent être visibles en haut."""
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
    assert "EMAIL #123 — client@example.com" in body
    assert "Dossier cockpit : https://example.com/app/conversation/123" in body
    # Le body original est déjà dans gen.draft → pas de bloc 📧 MAIL ORIGINAL DU CLIENT ajouté
    assert body.count("MESSAGE ORIGINAL DU CLIENT") == 1
    assert body.count("Bonjour, je souhaite un devis.") == 1


def test_build_draft_body_shows_email_id_and_original_mail_v1259() -> None:
    """v1.25.9 — le brouillon doit afficher EMAIL #id + email client + mail original."""
    incoming = IncomingMail(
        sender="Pw@deltenre.be",
        subject="Avis de livraison de colis",
        body="Nom: Willocx\nPrénom: Paul\nTéléphone: 0497419241\n\nJ'ai appris qu'un ouvrier travaillerait au noir.",
        received_at="Mon, 22 Jun 2026 14:05:00 +0000",
        message_id="<622@detective>",
    )
    gen = GenerationResult(
        draft="Bonjour Monsieur Willocx,\n\nMerci pour votre demande. Pourriez-vous me confirmer l'adresse du site ?",
        raw_draft="Bonjour Monsieur Willocx,\n\nMerci pour votre demande...",
        language="fr",
        rag_pairs=[],
        model_used="test",
        category="demande_client",
    )
    body = _build_draft_body(incoming, gen, mail_id=622, base_url="https://detective.digitalhs.biz")

    # Email client visible en haut
    assert "EMAIL #622 — Pw@deltenre.be" in body
    assert "Dossier cockpit : https://detective.digitalhs.biz/app/conversation/622" in body

    # Mail original présent avec expéditeur et sujet (draft ne le contient pas encore)
    assert "📧 MAIL ORIGINAL DU CLIENT" in body
    assert "De : Pw@deltenre.be" in body
    assert "Sujet : Avis de livraison de colis" in body
    assert "Nom: Willocx" in body
    assert "J'ai appris qu'un ouvrier travaillerait au noir." in body

    # Séparation claire entre original et brouillon
    assert "💬 BROUILLON DE RÉPONSE PROPOSÉ" in body
    assert "Bonjour Monsieur Willocx," in body


def test_build_draft_body_legacy_draft_without_original_gets_body_injected() -> None:
    """v1.25.9 — si le draft stocké ne contient pas le message original,
    incoming.body est injecté pour garantir le contexte complet."""
    incoming = IncomingMail(
        sender="client@example.com",
        subject="Demande",
        body="Bonjour, pouvez-vous m'aider ?\nNom: Dupont",
        received_at="Mon, 22 Jun 2026 14:00:00 +0000",
        message_id="<legacy@example.com>",
    )
    gen = GenerationResult(
        draft="Merci pour votre message. Pourriez-vous me préciser votre besoin ?",
        raw_draft="Merci pour votre message...",
        language="fr",
        rag_pairs=[],
        model_used="test",
        category="demande_client",
    )
    body = _build_draft_body(incoming, gen, mail_id=999, base_url="")
    assert "EMAIL #999 — client@example.com" in body
    assert "📧 MAIL ORIGINAL DU CLIENT" in body
    assert "De : client@example.com" in body
    assert "Sujet : Demande" in body
    assert "Nom: Dupont" in body
    assert "💬 BROUILLON DE RÉPONSE PROPOSÉ" in body
    assert "Merci pour votre message." in body
