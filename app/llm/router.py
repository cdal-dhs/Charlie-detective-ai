import os

import structlog
from litellm import acompletion

from app.alerts import alert_ollama_credit_low
from app.config import get_settings
from app.settings_store import get_llm_models

log = structlog.get_logger()

_ollama_alert_sent = False


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
        global _ollama_alert_sent
        err_str = str(e).lower()
        is_rate_limit = (
            "429" in err_str
            or "ratelimit" in err_str
            or "rate limit" in err_str
            or "usage limit" in err_str
        )
        if is_rate_limit and not _ollama_alert_sent:
            _ollama_alert_sent = True
            await alert_ollama_credit_low()
        log.warning("llm.primary_failed", model=model, error=str(e))
        if model == fallback_model:
            raise
        log.info("llm.fallback", fallback=fallback_model)
        # Fallback : config spécifique selon le provider
        fb_extra: dict = {}
        if fallback_model.startswith("openrouter/"):
            fb_extra["api_base"] = "https://openrouter.ai/api/v1"
            if settings.openrouter_api_key:
                fb_extra["api_key"] = settings.openrouter_api_key
        elif fallback_model.startswith("openai/") and settings.ollama_pro_base_url:
            # Modèles Ollama Cloud via endpoint OpenAI-compatible
            fb_extra["api_base"] = settings.ollama_pro_base_url
            if settings.ollama_pro_api_key:
                fb_extra["api_key"] = settings.ollama_pro_api_key
        resp = await acompletion(
            model=fallback_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **fb_extra,
        )
        content = resp.choices[0].message.content or ""
        log.info("llm.fallback_response", fallback=fallback_model, length=len(content))
        return content
