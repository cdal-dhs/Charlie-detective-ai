import os
import re

import structlog
from litellm import acompletion

from app.alerts import alert_ollama_credit_low
from app.config import get_settings
from app.settings_store import get_llm_models

log = structlog.get_logger()

_ollama_alert_sent = False

# Patterns de raisonnement typiques des modèles de type "reasoning" (kimi-k2.6:cloud, etc.)
# qui laissent des traces dans reasoning_content. On les filtre en post-traitement.
_REASONING_LINE_PATTERNS = [
    re.compile(r"^L'utilisateur\s+(me\s+)?demande\b", re.IGNORECASE),
    re.compile(r"^L'utilisateur\s+n'a\s+pas\b", re.IGNORECASE),
    re.compile(r"^Je\s+(dois|vais|peux|peux\s+pas|m'assure|réponds|rédige)\b", re.IGNORECASE),
    re.compile(r"^Réponse\s+possible\s*:", re.IGNORECASE),
    re.compile(r"^Points?\s+importants?\s*:", re.IGNORECASE),
    re.compile(r"^Points?\s+clés?\s*:", re.IGNORECASE),
    re.compile(r"^Ce\s+qu'il\s+faut\b", re.IGNORECASE),
    re.compile(r"^Structure\s+(possible|suggérée)?\s*:", re.IGNORECASE),
    re.compile(r"^Ton\s+\w+\s*:", re.IGNORECASE),
    re.compile(r"^Brouillon\s*:", re.IGNORECASE),
    re.compile(r"^Refonte\s*:", re.IGNORECASE),
    re.compile(r"^Exemple\s*:", re.IGNORECASE),
    re.compile(r"^Version\s+\w+\s*:", re.IGNORECASE),
    re.compile(r"^Mais\s+en\s+version\b", re.IGNORECASE),
    re.compile(r"^Voici\s+(comment|ma|le|la|les|un|une|ce|cela|le\s+résultat|la\s+réponse)\b", re.IGNORECASE),
    re.compile(r"^C'est\s+(une|un|le|la|les|plus|assez|direct|clair|prêt)\b", re.IGNORECASE),
    re.compile(r"^Cela\s+(répond|est|permet|donne|semble)\b", re.IGNORECASE),
    re.compile(r"^Il\s+faut\b", re.IGNORECASE),
    re.compile(r"^Note\s*:", re.IGNORECASE),
    re.compile(r"^Vérification\s*:", re.IGNORECASE),
    re.compile(r"^Formulation\s+proposée", re.IGNORECASE),
    re.compile(r"^Attendez\b", re.IGNORECASE),
    re.compile(r"^Attendons\b", re.IGNORECASE),
    re.compile(r"^Je\s+m'assure\b", re.IGNORECASE),
    re.compile(r"^Je\s+peux\b", re.IGNORECASE),
    re.compile(r"^\d+\.\s+", re.IGNORECASE),  # listes numérotées type "1. Ton..."
    re.compile(r"^[-•]\s+", re.IGNORECASE),  # listes à puces
    re.compile(r'^".*"$', re.IGNORECASE),  # ligne entre guillemets = exemple
]


def _is_reasoning_line(line: str) -> bool:
    """Vrai si la ligne ressemble à une trace de raisonnement du LLM."""
    stripped = line.strip()
    if not stripped:
        return False
    return any(p.match(stripped) for p in _REASONING_LINE_PATTERNS)


def _clean_reasoning(text: str) -> str:
    """Nettoie le texte des traces de raisonnement typiques (kimi-k2.6:cloud).

    Stratégie :
    1. Split en lignes
    2. Si une ligne matche un pattern de raisonnement, on l'enlève
    3. On enlève les paragraphes consécutifs de raisonnement (>2 lignes d'affilée)
    4. On garde la dernière "tranche" non-raisonnement
    5. Trim des lignes vides multiples
    """
    if not text:
        return text

    lines = text.split("\n")
    # Marquer les lignes de raisonnement
    cleaned: list[str] = []
    skip_until_blank = False
    for line in lines:
        if _is_reasoning_line(line):
            skip_until_blank = True
            continue
        if skip_until_blank:
            # continuer à skipper tant qu'on a des lignes de raisonnement consécutives
            if line.strip() == "":
                skip_until_blank = False
            continue
        cleaned.append(line)

    result = "\n".join(cleaned)

    # Stratégie 2 : si on a encore beaucoup de texte "méta" avant la réponse,
    # on garde seulement le dernier tiers
    if len(result) > 500:
        # Si on détecte un "saut" clair (plusieurs paragraphes non-mail au début),
        # on tronque au premier vrai paragraphe
        # Heuristique : un paragraphe de mail commence souvent par "Madame", "Monsieur",
        # "Cher", "Chère", "Bonjour", "Beste", "Geachte", "Dear", "Hello", "Bedankt",
        # ou directement par du contenu narratif.
        mail_starters = (
            "Madame", "Monsieur", "Cher", "Chère", "Bonjour", "Bonsoir",
            "Beste", "Geachte", "Dear", "Hello", "Hi ", "Bedankt", "Dank",
            "Merci pour", "Je vous", "Je vous remercie",
        )
        # Chercher le 1er paragraphe qui commence par un mail starter
        for starter in mail_starters:
            idx = result.find(f"\n{starter}")
            if idx > 0 and idx < len(result) * 0.7:
                # Si on trouve un starter dans les 70% du texte, on coupe avant
                result = result[idx + 1:].strip()
                break

    # Réduire les lignes vides multiples
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    # Dernière passe : tronquer après une signature si on détecte une auto-critique
    # post-mail (le LLM se corrige après avoir écrit le mail).
    # Patterns : "Version plus X :", "C'est mieux.", "C'est parfait.", "C'est bon.",
    # "En fait, ...", "Je préfère...", "Attendez", "Je vais reformuler"
    auto_critique = re.search(
        r"\n\s*(Version\s+\w+|En\s+fait|Je\s+préfère|Je\s+vais\s+reformuler|"
        r"C'est\s+(mieux|parfait|bon|direct|clair|plus\s+\w+)|"
        r"Attendez|Refonte|Alternative\s*:|Ou\s+alors\s*:)",
        result,
        re.IGNORECASE,
    )
    if auto_critique and auto_critique.start() > 100:
        # Couper juste avant la critique, garder la première version
        result = result[: auto_critique.start()].rstrip()

    return result


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
        # kimi-k2.6:cloud (reasoning) met sa réponse dans reasoning_content
        # et laisse content vide. On extrait des 2 sources, puis on nettoie
        # les traces de raisonnement.
        msg = resp.choices[0].message
        content = msg.content or ""
        if not content and getattr(msg, "reasoning_content", None):
            content = msg.reasoning_content
        content = _clean_reasoning(content)
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
        msg = resp.choices[0].message
        content = msg.content or ""
        if not content and getattr(msg, "reasoning_content", None):
            content = msg.reasoning_content
        content = _clean_reasoning(content)
        log.info("llm.fallback_response", fallback=fallback_model, length=len(content))
        return content
