import re
from email.message import Message

NEWSLETTER_HEADERS = ("List-Unsubscribe", "List-Id", "Precedence")
NEWSLETTER_KEYWORDS = (
    "unsubscribe",
    "désinscrire",
    "desinscrire",
    "se désinscrire",
    "mailing",
    "newsletter",
    "promotion",
    "offre spéciale",
    "abonnement",
    "mailchimp",
    "sendinblue",
    "brevo",
    "campaign",
    "ne plus recevoir",
    "paramètres d'e-mail",
    "parametres d'email",
    "préférences de communication",
    "mise à jour produit",
    "nouveauté",
    "témoignage client",
    "success story",
)
NEWSLETTER_SENDERS = (
    "mailchimp",
    "sendinblue",
    "brevo",
    "hubspot",
    "mailjet",
    "constantcontact",
    "getresponse",
    "activecampaign",
    "aweber",
    "convertkit",
    "klaviyo",
    "substack",
    "mailerlite",
)


def _get_body_snippet(msg: Message) -> str:
    """Extrait ~500 caractères du corps pour scan rapide."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")[:500].lower()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    text = re.sub(r"<[^>]+>", " ", payload.decode("utf-8", errors="replace"))
                    return text[:500].lower()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            return text[:500].lower()
    return ""


def is_newsletter(msg: Message) -> bool:
    # Headers typiques
    if any(msg.get(h) for h in NEWSLETTER_HEADERS):
        return True
    # Mots-clés dans le sujet
    subject = (msg.get("Subject", "") or "").lower()
    if any(kw in subject for kw in NEWSLETTER_KEYWORDS):
        return True
    # Mots-clés dans le corps
    body_snippet = _get_body_snippet(msg)
    if any(kw in body_snippet for kw in NEWSLETTER_KEYWORDS):
        return True
    # Expéditeur connu de plateforme mailing
    sender = (msg.get("From", "") or "").lower()
    if any(s in sender for s in NEWSLETTER_SENDERS):
        return True
    return False


def is_known_billing(msg: Message) -> bool:
    """TODO S2 : matcher l'expéditeur contre une liste blanche de domaines facturation
    (fournisseurs récurrents : OVH, Infomaniak, comptable, etc.). Charger depuis
    agent_state.db pour permettre l'enrichissement en prod."""
    return False


def quick_classify(msg: Message) -> str | None:
    """Retourne une catégorie si une règle évidente s'applique, sinon None
    (→ on délègue au LLM classifier)."""
    if is_newsletter(msg):
        return "newsletter"
    if is_known_billing(msg):
        return "facture"
    return None
