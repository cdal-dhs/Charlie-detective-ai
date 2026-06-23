"""Correction des sujets d'email incohérents ou illisibles.

v1.25.3 — Deux cas distincts chez Daniel :
- **Homoglyphes** (ex: #614 « іtѕⅿе-Bеvеіlіngѕmеldіng » = cyrillique + chiffre
  romain ⅿ ressemblant à « itsme-Bevelingsmelding ») : sujet illisible.
- **Sujet non-représentatif** (ex: #515 « [Privédetective België]
  Réinitialisation du mot de passe » = forwarder WordPress automatique) :
  le sujet est lisible mais **totalement incohérent** avec la vraie demande
  du client (qui est dans le body).

Correction par LLM (forfait Ollama Pro = coût nul) :
- `is_subject_suspect()` détecte les sujets contenant des confusables
  (cyrillique/grec/chiffres romains) censés être du Latin — détermination
  DÉTERMINISTE, fiable, zéro faux positif → utilisée par l'auto-pipeline.
- `fix_subject_llm()` demande au LLM un sujet propre, court, lisible ET
  représentatif de la demande réelle (lue dans le body). Réformule les sujets
  incohérents comme les homoglyphes. Utilisée par l'auto-pipeline (homoglyphes
  only) ET par le bouton cockpit (rétoocorrection manuelle de tout sujet
  incohérent, y compris non-homoglyphes comme #515).

Dégradation silencieuse : si le LLM échoue ou ne propose rien de mieux
(sujet déjà représentatif), on conserve le sujet original (jamais de crash).
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


# Forwarders WordPress : les formulaires WP n'exposent jamais l'email du client
# (vrai contact = téléphone, cf. Task #4). Répondre au forwarder ne reachera pas
# le client. On tag le sujet pour que Daniel/le brouillon le sache immédiatement.
_WP_FORWARDER_RE = re.compile(r"^(?:mail|wordpress|contact)@.*detective", re.IGNORECASE)
_NO_EMAIL_TAG = "[NO_EMAIL_IN_THE_FORM]"


def is_wp_forwarder(sender: str) -> bool:
    """True si l'expéditeur est un forwarder WordPress (mail@/wordpress@/contact@detective*).

    Ex: wordpress@detectivebelgium.com, mail@detectivebelgique.be,
    contact@detectivebelgium.com. Ces mails n'ont pas d'email client → le vrai
    contact est le téléphone (champ Telefoonnummer du formulaire).
    """
    return bool(_WP_FORWARDER_RE.match((sender or "").strip()))


def tag_no_email(subject: str, sender: str) -> str:
    """Suffixe le sujet avec [NO_EMAIL_IN_THE_FORM] si sender = forwarder WP.

    Idempotent : ne re-tag pas si le tag est déjà présent. Ne modifie pas les
    sujets de senders normaux (ex: #614 yashwantsharma@...). Retourne le sujet
    inchangé si pas un forwarder WP.
    """
    subject = subject or ""
    if not is_wp_forwarder(sender):
        return subject
    if _NO_EMAIL_TAG in subject:
        return subject
    return f"{subject} {_NO_EMAIL_TAG}".strip()


async def fix_subject_llm(subject: str, body_preview: str) -> str | None:
    """Demande au LLM un sujet propre, court, lisible ET représentatif de la demande.

    Couvre deux cas : (1) homoglyphes illisibles (#614), (2) sujet automatique
    non-représentatif (#515 forwarder WP « Réinitialisation du mot de passe »)
    où le LLM reformule à partir du body pour refléter la vraie demande.

    Retourne le sujet corrigé (str), ou None si le LLM échoue / renvoie le même
    sujet (déjà représentatif) / renvoie vide. L'appelant conserve l'original
    dans ce cas (dégradation silencieuse).
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
                "Tu corriges/réformules des sujets d'email incohérents ou "
                "illisibles : (1) homoglyphes (caractères cyrilliques/grecs/"
                "chiffres romains ressemblant à du Latin), (2) sujet automatique "
                "non-représentatif de la demande (ex: « Réinitialisation du "
                "mot de passe », « Contact form », forwarders). À partir du "
                "sujet original ET de l'extrait du corps, tu renvoies "
                "UNIQUEMENT un sujet propre, court, lisible, qui REFLETTE LA "
                "DEMANDE RÉELLE du client (lue dans le corps). En ASCII si "
                "possible (accents FR/NL autorisés), max 100 caractères, sans "
                "guillemets, sans préfixe « Sujet : », sur une seule ligne. "
                "Si le sujet reflète déjà correctement la demande, renvoie-le "
                "tel quel."
            ),
        },
        {
            "role": "user",
            "content": f"Sujet original :\n{subject}\n\n"
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
