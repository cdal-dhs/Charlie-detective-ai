from langdetect import LangDetectException, detect

# Codes BCP-47 (étendu à toutes langues) — pas de Literal pour rester flexible.
Language = str

# Langues explicitement supportées par le détecteur de Daniel (pour affichage multilingue
# des prompts RAG et la classification prioritaire des drafts).
DANIEL_LANGUAGES = ("fr", "nl", "en")


def detect_language(text: str, default: Language = "fr") -> Language:
    """Détecte la langue du texte. Retourne un code BCP-47 (fr, nl, en, es, de, it, ...).

    Fallback sur `default` si texte vide ou détection impossible.
    """
    if not text.strip():
        return default
    cleaned = text.replace("\n", " ")[:1000]
    try:
        return detect(cleaned)
    except LangDetectException:
        return default


def language_label(code: Language) -> str:
    """Libellé humain d'une langue pour affichage (ex: 'Néerlandais', 'Espagnol')."""
    labels = {
        "fr": "Français",
        "nl": "Néerlandais",
        "en": "Anglais",
        "de": "Allemand",
        "es": "Espagnol",
        "it": "Italien",
        "pt": "Portugais",
        "lb": "Luxembourgeois",
        "wa": "Wallon",
    }
    return labels.get(code, code.upper())
