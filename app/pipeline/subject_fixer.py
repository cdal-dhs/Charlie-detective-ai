"""Correction des sujets illisibles (homoglyphes itsme, cyrillique, etc.).

v1.25.3 — Daniel reçoit des mails au sujet truffé d'homoglyphes (ex: #614
« іtѕⅿе-Bеvеіlіngѕmеldіng » = cyrillique + chiffre romain ⅿ ressemblant à
« itsme-Bevelingsmelding »). Le sujet est illisible dans l'inbox et pollue
le sujet du brouillon V2a (« DEMANDE D'Approbation - ... : {sujet} »).

Correction par LLM (forfait Ollama Pro = coût nul) :
- `is_subject_suspect()` détecte les sujets contenant des confusables
  (cyrillique/grec/chiffres romains) censés être du Latin.
- `fix_subject_llm()` demande au LLM un sujet propre, court, lisible.

Dégradation silencieuse : si le LLM échoue ou ne propose rien de mieux,
on conserve le sujet original (jamais de crash).
"""

from __future__ import annotations

# Les caractères cyrilliques/chiffres romains ci-dessous sont intentionnels :
# ils documentent et testent la détection d'homoglyphes (ex: #614 itsme).
# ruff: noqa: RUF002, RUF003
import re

import structlog

from app.llm.router import complete
from app.settings_store import get_llm_models

log = structlog.get_logger()


# Plages Unicode de confusables : un sujet FR/NL/EN légitime n'en contient jamais.
# - Cyrillique U+0400–U+04FF (і е о а ѕ … ressemblant à i e o a s)
# - Grec U+0370–U+03FF (ο ρ ν … ressemblant à o p v)
# - Chiffres romains Unicode U+2160–U+2188 (ⅿ = m, ⅼ = l, etc.)
_CONFUSABLE_RE = re.compile(r"[Ͱ-ϿЀ-ӿⅠ-ↈ]")


def is_subject_suspect(subject: str) -> bool:
    """True si le sujet contient des confusables (cyrillique/grec/chiffres romains).

    Les accents Latin (é è à ç ñ …) ne sont PAS des confusables → False.
    """
    if not subject:
        return False
    return bool(_CONFUSABLE_RE.search(subject))


async def fix_subject_llm(subject: str, body_preview: str) -> str | None:
    """Demande au LLM un sujet lisible, propre, court.

    Retourne le sujet corrigé (str), ou None si le LLM échoue / ne propose
    rien de mieux / renvoie vide. L'appelant conserve le sujet original dans
    ce cas (dégradation silencieuse).
    """
    if not subject:
        return None
    model, _ = get_llm_models()
    # On tronque le body_preview pour rester léger (le sujet suffit en général,
    # mais le body aide le LLM à reformuler un sujet cohérent).
    body_hint = (body_preview or "")[:600]
    messages = [
        {
            "role": "system",
            "content": (
                "Tu corriges des sujets d'email illisibles (homoglyphes, "
                "caractères cyrilliques/grecs/chiffres romains ressemblant à "
                "du texte Latin). Tu renvoies UNIQUEMENT le sujet corrigé, "
                "propre, en ASCII si possible (accents FR/NL autorisés), "
                "max 100 caractères, sans guillemets, sans préfixe "
                "« Sujet : », sur une seule ligne. Si le sujet est déjà "
                "lisible, renvoie-le tel quel."
            ),
        },
        {
            "role": "user",
            "content": f"Sujet original illisible :\n{subject}\n\n"
            f"Extrait du corps (contexte) :\n{body_hint}\n\n"
            "Sujet corrigé :",
        },
    ]
    try:
        raw = await complete(model=model, messages=messages, max_tokens=120, temperature=0.2)
    except Exception as exc:  # dégradation silencieuse — ne jamais crasher le pipeline
        log.warning("subject_fixer.llm_failed", error=str(exc))
        return None

    if not raw:
        return None
    cleaned = _clean(raw)
    # Refus explicite / aucune amélioration : on garde l'original.
    if not cleaned or cleaned.lower() == subject.strip().lower():
        return None
    # Sécurité : ne pas renvoyer un sujet absurdement long (hallucination).
    if len(cleaned) > 200:
        return None
    return cleaned


def _clean(raw: str) -> str:
    """Nettoie la sortie LLM : retire guillemets, préfixes « Sujet : », whitespace."""
    s = raw.strip().strip('"').strip("'").strip("«").strip("»").strip()
    # Retire un éventuel préfixe « Sujet : » / « Subject: »
    s = re.sub(r"^(?:sujet|subject)\s*[:\-]\s*", "", s, flags=re.IGNORECASE)
    # Garde la première ligne seulement (évite les justifications LLM).
    s = s.splitlines()[0].strip() if s else ""
    return s
