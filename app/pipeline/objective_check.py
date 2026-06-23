"""Évaluation de la clarté de l'objectif final d'une demande client.

v1.25.6 — Cf. #615 (Andree Marie Scurbecq) : « faire une petite enquete au bureau
de douane de Kaiserslautern » = demande d'enquête SANS objectif final précis
(prouver quoi ? vérifier quoi ?). Le brouillon qualifiant standard sautait aux
tarifs sans demander l'objectif, or sans objectif on ne peut pas établir un
devis. Le brouillon « demande floue » (v1.25.1) existe déjà et demande
l'objectif — il n'était juste pas déclenché car la détection se basait sur la
longueur totale du body (gonflé par les champs formulaire + mentions légales).

Ce module fournit le verdict amont (objectif clair vs flou) basé sur le message
LIBRE du client (hors champs formulaire), par un HYBRIDE :

1. Heuristique déterministe (rapide, zéro LLM) : si le message libre contient un
   objectif final évident (filature, infidélité, surveillance, recherche,
   récupération de dette, micros, incapacité, harcèlement…), verdict = clair.
2. LLM gemma4 si l'heuristique est incertaine : « le client a-t-il exprimé un
   objectif final précis et actionnable ? » Multilingue (NL/EN/DE/ES…).
3. Dégradation : si le LLM échoue → flou (règle d'or du projet : faux positifs
   acceptables — demander l'objectif inutilement —, faux négatifs intolérables
   — rater une demande floue et livrer un devis sans objectif).

Coût nul (forfait Ollama Pro). Latence : l'heuristique est instantanée, le LLM
n'est appelé que pour les demandes sans objectif évident (précisément #615).
"""

from __future__ import annotations

import re

import structlog

from app.llm.router import complete
from app.settings_store import get_llm_models

log = structlog.get_logger()

# Frontière des champs formulaire : on coupe le body au premier champ client
# pour isoler le message libre du client. Cf. _INFO_STOP du qualification_builder.
_FORM_FIELD_RE = re.compile(
    r"\n\s*(?:Nom|Pr[ée]nom|T[ée]l[ée]phone|Email|E-mail|GSM|Adresse|Profil|"
    r"Heure\s+de\s+contact|Votre\s+profil|Votre\s+message|Mentions\s+l[ée]gales|"
    r"Ce\s+formulaire|Politique\s+de\s+confidentialit[ée]|Nom\s+complet|"
    r"Soci[ée]t[ée])\b\s*[:\-=?]",
    re.IGNORECASE,
)

# Objectifs finals ÉVIDENTS (cas Daniel). Présence d'un de ces termes dans le
# message libre = objectif clair → pas de LLM. On évite le générique « enquête »
# (trop large : « faire une petite enquête » ≠ objectif précis).
_CLEAR_OBJECTIVE_RE = re.compile(
    r"\b(?:"
    r"infid[ée]lit[ée]|filature|filer|surveiller|surveillance|"
    r"prouver|constater|d[ée]montrer|[ée]tablir\s+que|savoir\s+si|"
    r"retrouver|localiser|identifier|rechercher\s+(?:une\s+personne|quelqu'un|la\s+personne)|"
    r"r[ée]cup[ée]rer\s+(?:une\s+)?dette|dette|r[ée]clamer|"
    r"harc[èe]lement|violences?|pass[ée]\s+de\s+violences?|"
    r"micros?|cam[ée]ras?|espionnage|contre-?espionnage|"
    r"incapacit[ée]\s+de\s+travail|certificat\s+d'incapacit[ée]|"
    r"constat(?:er)?|preuve(?:s)?\s+de|fraude(?:uleuse|use)?|"
    r"adult[èe]re|cocufiage|tromperie|"
    r"garder\s+les\s+enfants|garde\s+des\s+enfants|"
    r"vol|d[ée]tournement|abus|escroquerie"
    r")\b",
    re.IGNORECASE,
)

# Question de tarif explicite = le client sait ce qu'il veut (sécurité : pas flou).
_TARIFF_QUESTION_RE = re.compile(
    r"(?:combien\s+(?:ça\s+)?co[ûu]t|quel(?:le)?\s+(?:est\s+)?(?:le\s+|votre\s+|vos\s+|du\s+|de\s+)?(?:prix|tarif|co[ûu]t)"
    r"|prix\s+[:?]|tarif\s+[:?]|what\s+(?:is\s+)?(?:the\s+)?(?:price|cost|rate)|how\s+(?:much|many)"
    r"|wat\s+kost|hoeveel|prijs|tarieven)",
    re.IGNORECASE,
)


def extract_free_message(body: str) -> str:
    """Retourne le message libre du client (avant les champs formulaire).

    Les formulaires WordPress collent le message du client puis les champs
    `Nom:`/`Prénom:`/`Téléphone:`/`Mentions légales:`… Le message libre (la vraie
    prose du client) est tout ce qui précède le premier champ. Cf. #615.
    """
    body = body or ""
    m = _FORM_FIELD_RE.search(body)
    free = body[: m.start()] if m else body
    return free.strip()


def _has_clear_objective_heuristic(free_msg: str) -> bool | None:
    """True si objectif évident, False si manifestement flou, None si incertain.

    - Question de tarif explicite → True (le client sait ce qu'il veut).
    - Objectif final évident (filature, infidélité, surveillance…) → True.
    - Message vide / lapidaire (< 60 chars) sans rien → False (flou).
    - Sinon → None (incertain, délégué au LLM).
    """
    free = free_msg or ""
    if not free:
        return False
    if _TARIFF_QUESTION_RE.search(free):
        return True
    if _CLEAR_OBJECTIVE_RE.search(free):
        return True
    if len(free) < 60:
        return False
    return None


async def _has_clear_objective_llm(free_msg: str) -> bool | None:
    """Verdict LLM : True (clair) / False (flou) / None (LLM échoue → dégradation)."""
    if not free_msg:
        return False
    model, _ = get_llm_models()
    messages = [
        {
            "role": "system",
            "content": (
                "Tu évalues une demande reçue par un détective privé belge. "
                "Le client a-t-il exprimé un OBJECTIF FINAL précis et actionnable — "
                "ce qu'il veut obtenir concrètement de l'intervention "
                "(prouver une infidélité, surveiller une personne, retrouver "
                "quelqu'un, constater un fait, récupérer une dette, vérifier une "
                "incapacité de travail, détecter des micros…) — ou juste une "
                "intention vague SANS but précis (ex: « faire une petite enquête », "
                "« j'ai besoin d'un détective » sans dire pourquoi) ? "
                "Réponds UNIQUEMENT par OBJECTIF_CLAIR ou OBJECTIF_FLOU, rien d'autre."
            ),
        },
        {
            "role": "user",
            "content": f"Message du client :\n{free_msg[:800]}\n\nVerdict :",
        },
    ]
    try:
        raw = await complete(model=model, messages=messages, max_tokens=20, temperature=0.1)
    except Exception as exc:  # dégradation silencieuse — ne jamais crasher le pipeline
        log.warning("objective_check.llm_failed", error=str(exc))
        return None

    if not raw:
        return None
    verdict = raw.strip().upper()
    if "CLAIR" in verdict and "FLOU" not in verdict:
        return True
    if "FLOU" in verdict:
        return False
    # Réponse inattendue → dégradation vers flou (règle d'or : faux positif acceptable).
    log.warning("objective_check.llm_unexpected", raw=raw.strip()[:80])
    return None


async def assess_objective_clarity(free_msg: str) -> bool:
    """Verdict final : True si objectif clair, False si flou.

    Hybride heuristique → LLM → dégradation vers flou (False) si tout échoue.
    Cf. #615. Le brouillon « demande floue » (qualification_builder) est déclenché
    quand False.
    """
    verdict = _has_clear_objective_heuristic(free_msg)
    if verdict is not None:
        log.info(
            "objective_check.heuristic",
            verdict=verdict,
            free_len=len(free_msg or ""),
        )
        return verdict
    verdict = await _has_clear_objective_llm(free_msg)
    if verdict is not None:
        log.info("objective_check.llm", verdict=verdict, free_len=len(free_msg))
        return verdict
    # Dégradation : on n'a pas pu trancher → flou par sécurité (règle d'or).
    log.info("objective_check.degraded_to_flou", free_len=len(free_msg))
    return False
