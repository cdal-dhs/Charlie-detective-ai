"""Classification du cas de figure prospect pour adapter la qualification.

v1.22.7 : petit module dédié qui lit le mail entrant et retourne le cas de figure
le plus probable (parmi 5 cas métier) + un indicateur de confiance. Le modèle
utilisé est configurable (par défaut gemma4:31b via Ollama Pro).
"""

from __future__ import annotations

import json
import re

import structlog

from app.llm.router import complete
from app.settings_store import get_llm_model_qualifier

log = structlog.get_logger()

CASE_TYPES = (
    "incapacite_travail",
    "infidelite_filature",
    "recherche_personne",
    "securite_passé_violences",
    "contre_espionnage_micros",
    "non_determine",
)


_CASE_PROMPT = """Tu es un assistant de qualification pour un détective privé belge.
Analyse le mail entrant ci-dessous et détermine le cas de figure principal parmi :

1. incapacite_travail : ouvrier en arrêt maladie suspecté de travail au noir.
2. infidelite_filature : surveillance / filature pour suspicion d'infidélité ou adultère.
3. recherche_personne : recherche d'une personne disparue ou demande d'adresse d'un tiers.
4. securite_passé_violences : vérification du passé d'un individu, antécédents violents.
5. contre_espionnage_micros : détection de micros/caméras
   ou installation de surveillance à domicile.
6. non_determine : aucun des cas précédents n'est clair.

Réponds UNIQUEMENT en JSON valide, sans markdown, sans commentaire :
{{
  "case_type": "...",
  "confidence": "high|medium|low",
  "reason": "une phrase courte justifiant le choix"
}}

Mail entrant :
De : {sender}
Sujet : {subject}
Corps :
{body}
"""


def _case_to_label(case_type: str) -> str:
    labels = {
        "incapacite_travail": "Ouvrier en incapacité de travail",
        "infidelite_filature": "Surveillance / infidélité",
        "recherche_personne": "Recherche de personne / adresse",
        "securite_passé_violences": "Passé de violences / sécurité",
        "contre_espionnage_micros": "Détection micros-caméras / installation",
        "non_determine": "Cas non déterminé",
    }
    return labels.get(case_type, case_type)


def _extract_case_type_from_json(text: str) -> tuple[str, str, str]:
    """Extrait le JSON de la réponse LLM, même si entouré de markdown."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        data = json.loads(text)
        case_type = data.get("case_type", "non_determine")
        confidence = data.get("confidence", "low")
        reason = data.get("reason", "")
        if case_type not in CASE_TYPES:
            case_type = "non_determine"
        return case_type, confidence, reason
    except json.JSONDecodeError:
        # Fallback : recherche textuelle
        lowered = text.lower()
        candidates = {
            "incapacite_travail": ["incapacité", "arrêt maladie", "travail au noir", "ouvrier"],
            "infidelite_filature": ["infidélité", "adultère", "filature", "surveillance", "couple"],
            "recherche_personne": ["disparu", "adresse", "retrouver", "localiser", "personne"],
            "securite_passé_violences": ["violence", "passé", "antécédent", "sécurité"],
            "contre_espionnage_micros": ["micro", "caméra", "espion", "contre-espionnage"],
        }
        scores = {case: sum(1 for kw in kws if kw in lowered) for case, kws in candidates.items()}
        best = max(scores, key=scores.get) if max(scores.values()) > 0 else "non_determine"
        return best, "low", "fallback par keyword"


async def classify_case(
    subject: str,
    body: str,
    sender: str = "",
) -> tuple[str, str, str]:
    """Détecte le cas de figure principal du mail entrant.

    Returns:
        (case_type, confidence, reason)
    """
    # Modèle dédié : settings store (runtime) > env > default
    model = get_llm_model_qualifier()

    prompt = _CASE_PROMPT.format(
        sender=sender or "?",
        subject=subject or "?",
        body=(body or "")[:2000],
    )
    try:
        raw = await complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        case_type, confidence, reason = _extract_case_type_from_json(raw)
    except Exception as exc:
        log.warning("case_classifier.failed", error=str(exc))
        return "non_determine", "low", ""

    log.info(
        "case_classifier.result",
        case=case_type,
        confidence=confidence,
        reason=reason,
        model=model,
    )
    return case_type, confidence, reason
