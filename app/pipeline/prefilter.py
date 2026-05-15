import re
from email.message import Message

# ── Newsletter ────────────────────────────────────────────────
NEWSLETTER_HEADERS = ("List-Unsubscribe", "List-Id", "Precedence")
NEWSLETTER_KEYWORDS = (
    "unsubscribe", "désinscrire", "desinscrire", "se désinscrire",
    "mailing", "newsletter", "promotion", "offre spéciale",
    "abonnement", "mailchimp", "sendinblue", "brevo", "campaign",
    "ne plus recevoir", "paramètres d'e-mail", "parametres d'email",
    "préférences de communication", "mise à jour produit", "nouveauté",
    "témoignage client", "success story", "bulletin", "lettre d'information",
    "decouvrez nos", "decouvrez nos offres", "promo", "black friday",
    "soldes", "remise", "code promo", "early access", "vip", "exclusive",
)
NEWSLETTER_SENDERS = (
    "mailchimp", "sendinblue", "brevo", "hubspot", "mailjet",
    "constantcontact", "getresponse", "activecampaign", "aweber",
    "convertkit", "klaviyo", "substack", "mailerlite", "campaign",
)

# ── Phishing ───────────────────────────────────────────────────
PHISHING_KEYWORDS = (
    "votre compte a été suspendu", "votre compte sera suspendu",
    "suspension immédiate", "confirmer votre identité",
    "vérifier votre compte", "valider vos informations",
    "mise à jour de sécurité requise", "fraude détectée",
    "activité suspecte", "accès non autorisé", "compte compromis",
    "phishing", "hameçonnage", "arnaque", "escroquerie",
    "cliquez sur le lien ci-dessous", "cliquez ici immédiatement",
    "votre mot de passe expire", "expiration imminente",
    "délai de 24h", "délai de 48h", "sanction", "pénalité",
    "réclamation en attente", "plainte déposée", "tribunal",
    "huissier", "gendarmerie", "police fédérale", "interpol",
)
PHISHING_SENDERS = (
    "noreply-", "no-reply-", "alerte-", "securite-", "security-",
    "support-urgent", "verification-", "confirm-", "validate-",
)
SUSPICIOUS_DOMAINS = (
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
)

# ── Rappel ─────────────────────────────────────────────────────
RAPPEL_KEYWORDS = (
    "rappel", "relance", "échéance", "echeance", "deadline",
    "impayé", "impaye", "non payé", "non paye", "en retard",
    "retard de paiement", "paiement en attente", "solde débiteur",
    "facture en souffrance", "dû depuis", "du depuis", "date limite",
    "dernier délai", "dernier rappel", "avant recours", "avant poursuite",
    "rendez-vous", "rdv", "rappel de rendez-vous", "rappel consultation",
    "convocation", "audience", "déposition", "deposition",
)

# ── Facture ──────────────────────────────────────────────────
FACTURE_KEYWORDS = (
    "facture", "invoice", "vat", "tva", "acompte", "devis",
    "pro forma", "proforma", "bon de commande", "purchase order",
    "pièce jointe : facture", "pièce jointe: facture",
    "facture n°", "facture numero", "numero de facture",
)
FACTURE_SENDERS = (
    "ovh", "infomaniak", "stripe", "paypal", "amazon",
    "microsoft", "google", "accounting", "comptable",
    "bureau comptable", "fiduciaire",
)


def _get_body_snippet(msg: Message) -> str:
    """Extrait ~1000 caractères du corps pour scan rapide."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")[:1000].lower()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    text = re.sub(r"<[^>]+>", " ", payload.decode("utf-8", errors="replace"))
                    return text[:1000].lower()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+", " ", text)
            return text[:1000].lower()
    return ""


def _has_attachment(msg: Message) -> bool:
    """Vérifie s'il y a une pièce jointe potentiellement dangereuse."""
    if msg.is_multipart():
        for part in msg.walk():
            filename = part.get_filename() or ""
            ctype = part.get_content_type() or ""
            if filename.lower().endswith((".exe", ".zip", ".js", ".vbs", ".scr", ".bat", ".cmd")):
                return True
            if ctype in ("application/x-msdownload", "application/x-executable"):
                return True
    return False


def _extract_links(body_snippet: str) -> list[str]:
    """Extrait les URLs du corps du mail."""
    return re.findall(r'https?://[^\s<>"\']+', body_snippet)


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


def is_phishing(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)
    sender = (msg.get("From", "") or "").lower()

    # Mots-clés forts dans sujet ou corps
    if any(kw in subject for kw in PHISHING_KEYWORDS):
        return True
    if any(kw in body_snippet for kw in PHISHING_KEYWORDS):
        return True

    # Expéditeur suspect
    if any(s in sender for s in PHISHING_SENDERS):
        # Vérifier s'il y a aussi un lien ou une demande d'action
        links = _extract_links(body_snippet)
        if links or "cliquez" in body_snippet or "identif" in body_snippet:
            return True

    # Spoofing d'adresse : expéditeur affiché != expéditeur réel
    from_header = msg.get("From", "")
    reply_to = msg.get("Reply-To", "")
    if reply_to and reply_to.lower() not in from_header.lower():
        # Si Reply-To pointe vers un domaine grand public suspect
        domain = reply_to.split("@")[-1].lower().strip(">)")
        if domain in SUSPICIOUS_DOMAINS:
            return True

    # Pièce jointe dangereuse
    if _has_attachment(msg):
        return True

    return False


def is_rappel(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)

    if any(kw in subject for kw in RAPPEL_KEYWORDS):
        return True
    if any(kw in body_snippet for kw in RAPPEL_KEYWORDS):
        return True
    return False


def is_facture(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)
    sender = (msg.get("From", "") or "").lower()

    if any(kw in subject for kw in FACTURE_KEYWORDS):
        return True
    if any(kw in body_snippet for kw in FACTURE_KEYWORDS):
        return True
    if any(s in sender for s in FACTURE_SENDERS):
        return True
    return False


def quick_classify(msg: Message) -> str | None:
    """Retourne une catégorie si une règle évidente s'applique, sinon None
    (→ on délègue au LLM classifier)."""
    if is_phishing(msg):
        return "phishing"
    if is_newsletter(msg):
        return "newsletter"
    if is_facture(msg):
        return "facture"
    if is_rappel(msg):
        return "rappel"
    return None
