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


def has_client_email_in_body(body: str) -> bool:
    """True si le body contient un email client (pas un forwarder WP interne).

    Les formulaires WP ne demandent jamais l'email ; quand un body contient
    quand même un @, c'est souvent une signature, un footer ou un forwarder.
    On considère qu'il y a un vrai email client si le domaine n'appartient pas
    à Detective.be et si ce n'est pas un no-reply/service.
    """
    body = body or ""
    own_domains = ("detectivebelgique.be", "detectivebelgium.com", "dpdhuinvestigations.be")
    for match in re.finditer(r"[^\s<]+@[^\s>\n,]+", body):
        email = match.group(0).strip("<>")
        if "@" not in email:
            continue
        local, _, domain = email.rpartition("@")
        domain = domain.lower()
        local = local.lower()
        if any(d in domain for d in own_domains):
            continue
        no_reply_local = ("no-reply", "noreply", "donotreply")
        if domain.startswith(no_reply_local) or local in no_reply_local:
            continue
        return True
    return False


def tag_no_email(subject: str, sender: str, body: str = "") -> str:
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
    # Si un vrai email client est présent dans le body, le forwarder devient moins
    # critique ; on ne tagge pas pour ne pas surcharger le sujet.
    if has_client_email_in_body(body):
        return subject
    return f"{subject} {_NO_EMAIL_TAG}".strip()


def mask_forwarder_sender(sender: str, body: str = "", reply_to: str = "") -> str:
    """Retourne l'expéditeur affiché pour le brouillon/notification.

    v1.25.22 — priorité au header ``reply_to`` : pour les forwarders WP, le vrai
    email client est dans le Reply-To (cas #629 : ckremp@vo.lu = Christèle). On
    l'affiche à la place du forwarder technique.

    Sans Reply-To valide, si c'est un forwarder WP sans email client visible dans
    le body, on remplace par NO_EMAIL_IN_THE_FORM pour que Daniel comprenne qu'il
    ne doit PAS répondre à cette adresse technique.
    """
    rt = (reply_to or "").strip().strip("<>")
    if rt and "@" in rt and not _is_internal_address(rt):
        return rt
    if not is_wp_forwarder(sender):
        return sender or ""
    if has_client_email_in_body(body):
        return sender or ""
    return "NO_EMAIL_IN_THE_FORM"


def _is_internal_address(email: str) -> bool:
    """True si l'adresse appartient au cabinet Detective ou est un no-reply."""
    lowered = (email or "").lower()
    if lowered.startswith("no-reply") or lowered.startswith("noreply"):
        return True
    own_domains = ("detectivebelgique.be", "detectivebelgium.com", "dpdhuinvestigations.be")
    return any(d in lowered for d in own_domains)


async def fix_subject_llm(
    subject: str,
    body_preview: str,
    use_body_hint: bool = True,
) -> str | None:
    """Demande au LLM un sujet propre, court, lisible.

    Deux modes :
    - ``use_body_hint=True`` (défaut, bouton cockpit manuel) : reformule un sujet
      incohérent/non-représentatif (#515 « Réinitialisation mot de passe ») à
      partir du body pour refléter la vraie demande. Validé par un humain.
    - ``use_body_hint=False`` (auto-pipeline poller) : **translittère UNIQUEMENT**
      les homoglyphes (cyrillique/grec/chiffres romains) en Latin équivalent, en
      gardant le sens du sujet original. Ne regarde JAMAIS le body — sinon un
      body pollué par le chrome marketing d'un forwarder WP (cas #629
      « Envie de vous lancer... ») peut devenir le sujet stocké.

    Retourne le sujet corrigé (str), ou None si le LLM échoue / renvoie le même
    sujet (déjà représentatif) / renvoie vide. L'appelant conserve l'original
    dans ce cas (dégradation silencieuse).
    """
    if not subject:
        return None
    model, _ = get_llm_models()
    body_hint = (body_preview or "")[:600] if use_body_hint else ""
    if use_body_hint:
        system_prompt = (
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
        )
        user_content = (
            f"Sujet original :\n{subject}\n\n"
            f"Extrait du corps (contexte) :\n{body_hint}\n\n"
            "Sujet corrigé :"
        )
    else:
        # Auto-pipeline : translittération ONLY. On ne dérive jamais le sujet du
        # body (risque de pollution par le HTML marketing d'un forwarder, cas #629).
        system_prompt = (
            "Tu rétablis la lisibilité d'un sujet d'email contenant des "
            "homoglyphes (caractères cyrilliques/grecs/chiffres romains "
            "ressemblant à du Latin, ex: іtѕⅿе → itsme). Tu translittères "
            "UNIQUEMENT ces confusables en lettres Latin équivalentes, en "
            "conservant le sens et la ponctuation du sujet original. Tu ne "
            "reformules PAS, tu n'inventes rien, tu ne regardes aucun corps. "
            "Renvoie UNIQUEMENT le sujet translittéré, sur une seule ligne, "
            "sans guillemets ni préfixe « Sujet : ». Si le sujet ne contient "
            "aucun confusable, renvoie-le tel quel."
        )
        user_content = f"Sujet original :\n{subject}\n\nSujet translittéré :"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
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
