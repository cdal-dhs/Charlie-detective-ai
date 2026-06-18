import re
from pathlib import Path
from typing import Literal

import structlog

from app.config import get_settings
from app.llm.router import complete
from app.settings_store import get_llm_model_classifier

log = structlog.get_logger()

Category = Literal[
    "demande_client",
    "facture",
    "newsletter",
    "spam",
    "phishing",
    "rappel",
    "urgent",
    "autre",
]
VALID_CATEGORIES: tuple[Category, ...] = (
    "demande_client",
    "facture",
    "newsletter",
    "spam",
    "phishing",
    "rappel",
    "urgent",
    "autre",
)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classifier_prompt.txt"

# v1.22.1 — durcissement anti-oubli demande_client
# Le LLM se laisse parfois abuser par un "Re:" + citation d'un devis passé (mail #504).
# Si on a un soupçon de demande humaine + sender non-service, on force demande_client.
# Coût : 1-2 faux positifs (mail classé à tort) qu'on rattrape avec _is_verified_demande_client.
# Gain : on ne rate plus AUCUN client.
_HUMAN_QUESTION_SIGNALS = (
    # Question / suivi client typique (FR)
    "c'est combien", "c combien", "quel est le tarif", "quel est le prix",
    "quels sont vos tarifs", "vos tarifs", "votre prix", "votre tarif",
    "tarif exact", "prix exact", "devis", "combien ça coûte", "combien cela coûte",
    "combien coûte", "je vous contacte", "je vous écris", "je vous sollicite",
    "je souhaite", "je voudrais", "je cherche", "je désire", "j'aimerais",
    "est-ce que vous", "pouvez-vous", "pourriez-vous", "merci de me",
    "merci de bien vouloir", "dans l'attente", "en attente de votre",
    "j'attends de vos", "j'attends votre", "merci de votre retour",
    "merci de votre réponse", "pourriez-vous m'", "pouvez-vous m'",
    "avez-vous eu", "avez-vous bien reçu", "suivi de ma demande",
    "faire suite à", "fais suite à", "suite à votre", "suite à ma",
    "relance", "relancer", "nouvelles de", "des nouvelles",
    "avez-vous", "as-tu", "puis-je", "est-il possible",
    # Équivalents NL (néerlandais) — Detective Belgium
    "wens ik", "wenst u", "wilt u", "zou u", "kan u", "kunt u",
    "offerte", "prijs", "kost", "kosten", "tarief",
    "ik wil", "ik zou", "ik had", "ik heb", "alvast bedankt",
    "met vriendelijke groet", "mvg", "vraag", "vraagje",
    # Équivalents EN (anglais) — multilingue
    "i would like", "i want", "i need", "how much", "what is the cost",
    "could you", "would you", "please send", "quote", "i'm looking",
    "looking for", "kind regards", "regards", "best regards",
    # Questions à la 1ère personne sur l'enquête (typique Detective.be)
    "ma femme", "mon mari", "ma conjointe", "mon conjoint", "mon partenaire",
    "mon entreprise", "ma société", "mon employé", "ma salariée",
    "suspecte", "soupçonne", "soupçonn", "douter", "doute",
    "infidèle", "infidélité", "tromper", "adultère",
    "filature", "surveillance", "enquête", "investigation",
    "enquete", "détective", "detective", "privé",
    "mijn vrouw", "mijn man", "mijn partner",  # NL
    "my wife", "my husband", "my partner",  # EN
    # Verbes d'action métier
    "vérifier", "controler", "contrôler", "découvrir", "prouver",
    "confirmer", "identifier", "retrouver", "localiser",
)

_HUMAN_HINTS_IN_BODY = (
    # Présence d'un prénom/nom en début de mail
    "bonjour", "bonsoir", "hello", "hi ",
    "cher monsieur", "chère madame", "cher m.", "chère mme",
    "geachte", "beste", "beste heer", "beste mevrouw",
    "monsieur hurchon", "madame hurchon", "mr hurchon", "mme hurchon",
    "m. hurchon",
)


def _looks_like_human_question(body: str, subject: str, sender: str) -> bool:
    """Renvoie True si on a des indices forts qu'un humain pose une question
    ou fait un suivi au cabinet. Utilisé en garde-fou pour forcer demande_client
    quand le LLM hésite."""
    text = f"{subject}\n{body}".lower()
    sender_l = sender.lower()
    subject_l = subject.lower()
    body_l = body.lower()

    # Pas une demande humaine si :
    # 1. sender = service / no-reply / infomaniak / microsoft / google / etc.
    service_senders = (
        "noreply", "no-reply", "ne-pas-repondre", "donotreply",
        "infomaniak", "microsoft", "google", "apple", "meta", "facebook",
        "linkedin", "twitter", "x.com", "github", "gitlab", "atlassian",
        "stripe", "paypal", "ovh", "sendgrid", "mailgun", "brevo", "mailchimp",
        "hubspot", "zendesk", "intercom", "freshdesk",
        "support@", "billing@", "invoice@", "facture@", "compta@",
        "accounting@", "newsletter@", "promo@", "marketing@",
        # Expéditeurs de plateformes publicitaires / corporate connues.
        "ads-google", "googleads", "google-ads", "bauermedia", "outdoor.com",
    )
    if any(s in sender_l for s in service_senders):
        return False

    # 2. mots-clés de service évidents dans le sujet
    service_subjects = (
        "facture", "invoice", "recu", "reçu", "payment", "paiement",
        "subscription", "abonnement", "renewal", "renouvellement",
        "validation en deux étapes", "2-step", "two-step",
        "alerte de sécurité", "security alert", "new sign-in", "nouvelle connexion",
        "votre identifiant a été utilisé", "votre compte",
        "rapport de maintenance", "votre reçu", "your receipt",
    )
    if any(s in subject_l for s in service_subjects):
        return False

    # 3. Email automatique / corporate évident dans le body.
    auto_body_markers = (
        "dear customer",
        "dear user",
        "dear advertiser",
        "dear partner",
        "the google ads team",
        "the google team",
        "google llc",
        "1600 amphitheatre parkway",
        "privacy-enhancing technologies",
        "platform program policies",
        "eu user consent policy",
        "transparency and consent framework",
    )
    if any(m in body_l for m in auto_body_markers):
        return False

    # 4. Assez de contenu pour être une vraie question
    # On combine subject + body : si le subject seul est "Re:" et le body fait 2 chars,
    # ce n'est pas une vraie question.
    body_only = body.strip()
    if len(body_only) < 15 and len(subject.strip()) < 10:
        return False
    if len(f"{subject}\n{body}".strip()) < 15:
        return False

    # 5. "Re:" + sujet purement transactionnel/documentaire = pas une nouvelle demande.
    # Un vrai prospect répond à un devis existant en posant une question directe.
    if subject_l.startswith("re:") or subject_l.startswith("re :"):
        # Si le sujet ne contient qu'un nom de document/état sans verbe/question.
        transactional_re = re.compile(
            r"^re\s*:?\s*(devis|facture|provision|avenant|contrat|commande|offre|bon\s+de\s+commande)",
            re.IGNORECASE,
        )
        if transactional_re.search(subject):
            return False

    # Indices positifs : 1 signal fort ou 2 hints faibles
    hits = sum(1 for sig in _HUMAN_QUESTION_SIGNALS if sig in text)
    hints = sum(1 for h in _HUMAN_HINTS_IN_BODY if h in text)
    return hits >= 1 or hints >= 2


def _enforce_recall_over_precision(
    llm_category: str, subject: str, body: str, sender: str
) -> Category:
    """v1.22.1 — post-traitement non-négociable.

    Règle absolue CDAL : on ne rate AUCUN demande_client. Faux positifs acceptables
    (max 1-2%), faux négatifs intolérables (Daniel perd des clients).

    Si le LLM hésite vers autre/facture/rappel/urgent MAIS qu'on a des indices
    humains forts → on force demande_client.
    """
    if llm_category == "demande_client":
        return "demande_client"

    # Catégories où on tolère la remontée vers demande_client
    # (on ne remonte PAS depuis phishing/spam/newsletter — trop dangereux)
    upgradable = {"autre", "facture", "rappel", "urgent"}
    if llm_category not in upgradable:
        return llm_category  # type: ignore[return-value]

    if _looks_like_human_question(body, subject, sender):
        log.info(
            "classifier.recall_override",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    return llm_category  # type: ignore[return-value]


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


async def classify(subject: str, body: str, sender: str) -> Category:
    prompt = _load_prompt().format(subject=subject, body=body[:2000], sender=sender)
    response = await complete(
        model=get_llm_model_classifier(),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=15,
        temperature=0.0,
    )
    raw = response.strip().lower().split()[0] if response.strip() else "autre"
    if raw not in VALID_CATEGORIES:
        log.warning("classifier.invalid_response", raw=raw)
        return _enforce_recall_over_precision("autre", subject, body, sender)

    # v1.22.1 — post-traitement qui force demande_client en cas de doute
    return _enforce_recall_over_precision(raw, subject, body, sender)  # type: ignore[arg-type]
