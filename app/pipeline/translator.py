"""Traductions pour aide à la lecture Daniel.

Quand un mail arrive dans une langue ≠ FR :
- on traduit le mail entrant vers le FR (pour que Daniel lise)
- on traduit la proposition de réponse (FR) vers la langue du client (pour copie-coller)

Garde-fous : try/except systématique, on ne casse JAMAIS le pipeline.
Retour vide ('') en cas d'échec + log warning structuré.
"""

import structlog

from app.llm.router import complete
from app.pipeline.language import Language, language_label
from app.settings_store import get_llm_models

log = structlog.get_logger()

# Garde-fou : on tronque les très longs mails pour éviter timeout LLM.
MAX_TRANSLATION_CHARS = 12000


def _truncate(text: str) -> str:
    if len(text) <= MAX_TRANSLATION_CHARS:
        return text
    cut = MAX_TRANSLATION_CHARS
    return text[:cut] + "\n\n[...texte original tronqué pour la traduction...]"


async def translate_to_fr(text: str, source_lang: Language) -> str:
    """Traduit `text` depuis `source_lang` vers le français.

    Retourne '' en cas d'échec. Ne lève jamais.
    """
    if source_lang == "fr" or not text.strip():
        return ""
    try:
        llm_default, _ = get_llm_models()
        label = language_label(source_lang)
        messages = [
            {
                "role": "system",
                "content": (
                    f"Tu es un traducteur professionnel. Tu traduis fidèlement du {label} vers le français. "
                    "Tu conserves le ton, les formules de politesse, les dates et montants. "
                    "Tu ne fais AUCUN commentaire, AUCUNE interprétation. Tu renvoies UNIQUEMENT la traduction."
                ),
            },
            {
                "role": "user",
                "content": _truncate(text),
            },
        ]
        result = await complete(
            model=llm_default,
            messages=messages,
            max_tokens=3000,
            temperature=0.1,
        )
        log.info("translator.to_fr", source=source_lang, length=len(result))
        return result.strip()
    except Exception as e:
        log.warning("translator.to_fr_failed", source=source_lang, error=str(e))
        return ""


async def translate_from_fr(text: str, target_lang: Language) -> str:
    """Traduit `text` depuis le français vers `target_lang`.

    Retourne '' en cas d'échec. Ne lève jamais.
    """
    if target_lang == "fr" or not text.strip():
        return ""
    try:
        llm_default, _ = get_llm_models()
        label = language_label(target_lang)
        messages = [
            {
                "role": "system",
                "content": (
                    f"Tu es un traducteur professionnel. Tu traduis fidèlement du français vers le {label}. "
                    "Tu conserves le ton, les formules de politesse, les dates et montants. "
                    "Règle absolue : quand le texte contient une liste à puces avec des tarifs ou montants, "
                    "tu la réécris d'abord sous forme de phrases continues en français, puis tu traduis ces phrases. "
                    "Tu ne supprimes AUCUN tarif ni AUCUN montant. "
                    "Tu ne fais AUCUN commentaire, AUCUNE interprétation. Tu renvoies UNIQUEMENT la traduction complète."
                ),
            },
            {
                "role": "user",
                "content": _truncate(text),
            },
        ]
        result = await complete(
            model=llm_default,
            messages=messages,
            max_tokens=4000,
            temperature=0.1,
        )
        log.info("translator.from_fr", target=target_lang, length=len(result))
        return result.strip()
    except Exception as e:
        log.warning("translator.from_fr_failed", target=target_lang, error=str(e))
        return ""
