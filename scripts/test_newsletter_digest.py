"""Test one-shot du digest newsletter : scan la DB et envoie le résumé de la veille.

Usage : venv/bin/python -m scripts.test_newsletter_digest
"""

import asyncio
import sys

from app.workers.newsletter_digest import run_daily_digest


async def main() -> int:
    try:
        await run_daily_digest()
        print("Digest envoyé avec succès.")
        return 0
    except Exception as e:
        print(f"Erreur : {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
