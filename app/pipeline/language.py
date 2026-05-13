from typing import Literal

from langdetect import detect, LangDetectException

Language = Literal["fr", "nl", "en"]


def detect_language(text: str, default: Language = "fr") -> Language:
    if not text.strip():
        return default
    cleaned = text.replace("\n", " ")[:1000]
    try:
        code = detect(cleaned)
        if code in ("fr", "nl", "en"):
            return code  # type: ignore[return-value]
    except LangDetectException:
        pass
    return default
