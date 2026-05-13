from pathlib import Path
from typing import Literal

import structlog

from app.config import get_settings
from app.llm.router import complete

log = structlog.get_logger()

Category = Literal["demande_client", "facture", "newsletter", "spam", "urgent", "autre"]
VALID_CATEGORIES: tuple[Category, ...] = (
    "demande_client",
    "facture",
    "newsletter",
    "spam",
    "urgent",
    "autre",
)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classifier_prompt.txt"


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


async def classify(subject: str, body: str, sender: str) -> Category:
    settings = get_settings()
    prompt = _load_prompt().format(subject=subject, body=body[:2000], sender=sender)
    response = await complete(
        model=settings.llm_model_classifier,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=10,
        temperature=0.0,
    )
    raw = response.strip().lower().split()[0] if response.strip() else "autre"
    if raw not in VALID_CATEGORIES:
        log.warning("classifier.invalid_response", raw=raw)
        return "autre"
    return raw  # type: ignore[return-value]
