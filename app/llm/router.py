import os

import structlog
from litellm import acompletion

from app.config import get_settings
from app.settings_store import get_llm_models

log = structlog.get_logger()


def _ensure_env() -> None:
    """LiteLLM lit ses clés via env vars. On les expose depuis nos settings."""
    settings = get_settings()
    if settings.ollama_pro_api_key:
        if not os.environ.get("OLLAMA_API_KEY"):
            os.environ["OLLAMA_API_KEY"] = settings.ollama_pro_api_key
        if not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = settings.ollama_pro_api_key
    if settings.openrouter_api_key and not os.environ.get("OPENROUTER_API_KEY"):
        os.environ["OPENROUTER_API_KEY"] = settings.openrouter_api_key


async def complete(
    model: str,
    messages: list[dict],
    max_tokens: int = 1000,
    temperature: float = 0.3,
) -> str:
    """Wrapper LiteLLM avec fallback automatique vers le modèle de secours en cas d'échec."""
    _ensure_env()
    settings = get_settings()
    default_model, fallback_model = get_llm_models()
    extra: dict = {}
    if model.startswith("ollama_chat/") or model.startswith("ollama/"):
        extra["api_base"] = settings.ollama_pro_base_url
    elif model.startswith("openai/") and settings.ollama_pro_base_url:
        extra["api_base"] = settings.ollama_pro_base_url
        if settings.ollama_pro_api_key:
            extra["api_key"] = settings.ollama_pro_api_key

    try:
        resp = await acompletion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **extra,
        )
        content = resp.choices[0].message.content or ""
        log.info("llm.response", model=model, length=len(content))
        return content
    except Exception as e:
        log.warning("llm.primary_failed", model=model, error=str(e))
        if model == fallback_model:
            raise
        log.info("llm.fallback", fallback=fallback_model)
        resp = await acompletion(
            model=fallback_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        log.info("llm.fallback_response", fallback=fallback_model, length=len(content))
        return content
