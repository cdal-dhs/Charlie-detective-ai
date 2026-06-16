"""Test local d'un brouillon qualifiant — 0 email envoyé.

Ce script appelle directement `generate_draft()` sans passer par IMAP.
Les appels RAG et Cerveau2 sont mockés (réponses vides) pour ne pas dépendre
de l'état des bases. Le classifier de cas (LLM dédié) est appelé en vrai.

Usage :
    venv/bin/python -m scripts.test_draft_qualification
    venv/bin/python -m scripts.test_draft_qualification --case incapacite_travail
    venv/bin/python -m scripts.test_draft_qualification --subject "..." --body "..."
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch

from app.config import MailboxConfig, get_settings
from app.pipeline.generator import GenerationResult, generate_draft

log = structlog.get_logger()

CASES = {
    "filature_collaborateur": {
        "subject": "Demande de filature - collaborateur",
        "body": (
            "Bonjour,\n\n"
            "Je souhaiterais mettre en place une filature concernant "
            "l'un de mes collaborateurs à la sortie de son lieu de travail.\n\n"
            "Il semble repartir avec du matériel / de la marchandise chaque jour et "
            "j'ai besoin de preuves concrètes pour étayer ces faits.\n\n"
            "Ma société, MYCOMPANY_TEST, est située à Louvain-la-Neuve.\n\n"
            "Pourriez-vous m'indiquer si vous pouvez m'accompagner dans cette démarche "
            "et quelles seraient vos modalités d'intervention ?\n\n"
            "Cordialement,\n"
            "Christophe Dupont\n"
            "Directeur des opérations\n"
        ),
    },
    "incapacite_travail": {
        "subject": "Suspicion arrêt maladie - ouvrier",
        "body": (
            "Bonjour,\n\n"
            "Je pense qu'un de mes ouvriers est en arrêt maladie mais travaille au noir "
            "sur un chantier à côté de chez lui.\n\n"
            "Pouvez-vous vérifier cette incapacité de travail ?\n\n"
            "Cordialement,\n"
            "Pierre Martin\n"
        ),
    },
    "recherche_personne": {
        "subject": "Retrouver une personne disparue",
        "body": (
            "Bonjour,\n\n"
            "Je cherche à retrouver mon frère dont j'ai perdu le contact. Il habitait "
            "à Namur. Je n'ai plus son adresse actuelle.\n\n"
            "Pouvez-vous m'aider ?\n\n"
            "Bien à vous,\n"
            "Sophie Dubois\n"
        ),
    },
    "micros": {
        "subject": "Contrôle micro / caméra espion",
        "body": (
            "Bonjour,\n\n"
            "Je soupçonne mon ex-mari d'avoir placé des micros ou caméras dans mon appartement.\n\n"
            "Pouvez-vous venir faire une détection ?\n\n"
            "Cordialement,\n"
            "Marie Lefebvre\n"
        ),
    },
    "non_determine": {
        "subject": "Besoin d'aide",
        "body": (
            "Bonjour,\n\n"
            "J'ai besoin d'un détective pour une affaire personnelle. "
            "Pouvez-vous me contacter ?\n\n"
            "Merci,\n"
            "Jean Test\n"
        ),
    },
}


def _make_mailbox() -> MailboxConfig:
    settings = get_settings()
    return settings.mailboxes()[0]


async def _run(subject: str, body: str, category: str = "demande_client") -> GenerationResult:
    mailbox = _make_mailbox()

    # On moche RAG + Cerveau2 pour un test rapide et déterministe sur le builder.
    with (
        patch("app.pipeline.generator.retrieve", return_value=[]),
        patch("app.pipeline.generator.query_vault", return_value=([], "")),
    ):
        result = await generate_draft(
            incoming_subject=subject,
            incoming_body=body,
            sender="test@example.com",
            mailbox=mailbox,
            language="fr",
            category=category,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Test local d'un brouillon qualifiant")
    parser.add_argument(
        "--case",
        choices=list(CASES.keys()),
        default="filature_collaborateur",
        help="Cas de test prédéfini",
    )
    parser.add_argument("--subject", help="Sujet personnalisé")
    parser.add_argument("--body", help="Corps personnalisé")
    parser.add_argument(
        "--category",
        default="demande_client",
        help="Catégorie déclenchant le brouillon (défaut: demande_client)",
    )
    args = parser.parse_args()

    if args.subject and args.body:
        subject, body = args.subject, args.body
    else:
        case = CASES[args.case]
        subject, body = case["subject"], case["body"]

    result = asyncio.run(_run(subject, body, args.category))

    print("=" * 80)
    print(f"CAS        : {args.case}")
    print(f"CATEGORIE  : {args.category}")
    print(f"SUJET      : {subject}")
    print("=" * 80)
    print("\nBROUILLON :\n")
    print(result.draft)
    print("=" * 80)
    print(f"\nLangue détectée : {result.language}")
    print(f"Longueur        : {len(result.draft)} caractères")
    return 0


if __name__ == "__main__":
    sys.exit(main())
