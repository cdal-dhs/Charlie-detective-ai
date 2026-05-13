"""Smoke test LLM : appelle Kimi K2 via LiteLLM avec un prompt simple.

Pré-requis :
- Clé Ollama Pro valide dans .env (OLLAMA_PRO_API_KEY)
- URL Ollama Pro correcte (OLLAMA_PRO_BASE_URL, défaut https://ollama.com/api)
- Dépendances installées : litellm, pydantic-settings

Si la clé est vide, le script est skipped proprement sans appel API.
"""

import asyncio
import sys

import structlog

from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

PROMPT_MESSAGES: list[dict] = [
    {"role": "system", "content": "Tu es un assistant concis."},
    {"role": "user", "content": "Réponds uniquement par le mot 'pong'."},
]


async def main() -> int:
    settings = get_settings()

    if not settings.ollama_pro_api_key and not settings.openrouter_api_key:
        log.warning("smoke.llm.skipped", reason="aucune_cle_llm")
        print("Clé Ollama et OpenRouter manquantes — smoke test skipped")
        return 0

    model = settings.llm_model_default if settings.ollama_pro_api_key else settings.llm_model_fallback
    log.info("smoke.llm.start", model=model)

    try:
        resp = await complete(model=model, messages=PROMPT_MESSAGES, max_tokens=10, temperature=0.0)
    except Exception as e:
        log.error("smoke.llm.failed", error=str(e))
        print(f"FAIL : erreur LLM — {e}")
        return 1

    log.info("smoke.llm.result", response=resp)
    print(f"Réponse LLM ({model}) : {resp}")
    print("OK : smoke test LLM terminé avec succès")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
