import re
import unicodedata
from email.message import Message
from email.utils import parseaddr

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
# Sous-domaines marketing : les vraies demandes clients ne viennent jamais
# de info.*/news.*/email.* (plateformes d'envoi type Eloqua, Mailchimp, etc.).
NEWSLETTER_DOMAINS = (
    "info.", "news.", "newsletter.", "email.", "marketing.",
    "communications.", "mailing.", "campaign.", "edm.",
)
# Signatures URL de plateformes marketing dans le body (détection robuste,
# indépendante du sender). Eloqua = elqTrackId/elqaid, Mailchimp = mc_cid/mc_eid.
NEWSLETTER_MARKETING_URLS = (
    "elqtrackid", "elqaid", "elq=",
    "mc_cid", "mc_eid",
    "xtrk=", "trk_",
)


def _unaccent(s: str) -> str:
    """Normalise en ASCII (découvrez → decouvrez) pour matching accent-insensible."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

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

# ── Autre / à ignorer ─────────────────────────────────────────
AUTRE_KEYWORDS = (
    "updated invitation", "invitation updated", "calendar",
    "ical", "vcalendar", "event invitation", "meeting request",
    "accepté", "refusé", "tentative", "provisoire",
    "notification", "noreply", "no-reply", "donotreply",
    # Emails automatiques de services / transactions
    # NOTE : "renouvellement" retiré — géré par is_facture() quand combiné avec "facture"
    "renewal", "votre abonnement", "your subscription",
    "confirmation de", "confirmation de commande", "confirmation de paiement",
    "reçu de", "reçu de paiement", "payment receipt", "receipt",
    "votre commande", "your order", "commande confirmée",
    "mise à jour de votre", "mise a jour de votre", "update to your",
    "état de votre", "etat de votre", "status of your",
    "facture disponible", "invoice available", "your invoice",
    "alerte de sécurité", "security alert", "connexion détectée", "new sign-in",
    "2-step verification", "authentification à deux facteurs", "code de vérification",
    "bienvenue", "welcome to", "création de compte", "account created",
    "désabonnement", "désinscription", "unsubscribe", "subscription canceled",
    "votre espace client", "your customer area", "portail client",
    "message automatique", "email automatique", "automatic email", "do not reply",
)

SERVICE_SENDERS = (
    "infomaniak", "ovh", "stripe", "paypal", "amazon", "microsoft",
    "google", "apple", "meta", "facebook", "linkedin", "twitter", "x.com",
    "github", "gitlab", "sendgrid", "mailgun", "brevo", "mailchimp",
    "hubspot", "zendesk", "intercom", "freshdesk", "noreply", "no-reply",
    "donotreply", "ne-pas-repondre", "ne pas répondre", "alerte", "notification",
)

# ── Demande client ────────────────────────────────────────────
DEMANDE_KEYWORDS = (
    "demande d'information", "demande de renseignement",
    "demande de devis", "demande de prix", "demande de suivi",
    "demande d'enquête", "demande d'enquete",
    "demande de mission", "demande de consultation",
    "filature", "détective privé", "detective prive",
    "surveillance", "investigation", "enquête privée", "enquete privee",
    "je souhaite", "je voudrais", "je cherche", "je désire",
    "prenons contact", "premier rendez-vous",
    "nouveau message de", "formulaire de contact",
)
DEMANDE_SUBJECTS = (
    "demande", "filature", "surveillance", "investigation",
    "enquête", "enquete", "mission", "devis",
    "consultation", "renseignement",
)
FORM_SUBJECTS = (
    "nouveau message de", "contact form", "formulaire de contact",
    "demande de contact", "prise de contact",
)

# ── WordPress contact form (detectivebelgium.com / detectivebelgique.be) ──
# v1.25.12 — tolérance zéro : un formulaire WP avec champs structurés est
# TOUJOURS une demande_client, avant toute autre règle du pré-filtre.
_WP_FORM_NL_FIELDS = ("achternaam:", "voornaam:", "telefoonnummer:")
_WP_FORM_FR_FIELDS = ("nom:", "prénom:", "téléphone:", "prenom:", "telephone:")
_WP_FORM_FR_MARKER = "votre profil"
_WP_FORM_NL_MARKER = "hoe kunnen wij u helpen"


# ── Facture ──────────────────────────────────────────────────
FACTURE_KEYWORDS = (
    "facture", "invoice", "vat", "tva", "acompte",
    "pro forma", "proforma", "bon de commande", "purchase order",
    "pièce jointe : facture", "pièce jointe: facture",
    "facture n°", "facture numero", "numero de facture",
    "renouvellement", "renewal", "renouveler", "régler", "regler",
    "paiement en ligne", "pay online", "règlement",
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
    # Matching accent-insensible : « découvrez » (body) == « decouvrez » (keyword)
    subject = _unaccent((msg.get("Subject", "") or "").lower())
    body_snippet = _unaccent(_get_body_snippet(msg))
    keywords = [_unaccent(kw) for kw in NEWSLETTER_KEYWORDS]
    if any(kw in subject for kw in keywords):
        return True
    if any(kw in body_snippet for kw in keywords):
        return True
    # Signatures URL de plateformes marketing (Eloqua, Mailchimp) dans le body
    if any(u in body_snippet for u in NEWSLETTER_MARKETING_URLS):
        return True
    # Expéditeur connu de plateforme mailing
    sender = (msg.get("From", "") or "").lower()
    if any(s in sender for s in NEWSLETTER_SENDERS):
        return True
    # Sous-domaines marketing : info.arval.com, news.entreprise.com, email.xxx
    addr = parseaddr(sender)[1]
    domain = addr.split("@")[-1] if "@" in addr else ""
    return bool(domain and any(domain.startswith(d) for d in NEWSLETTER_DOMAINS))


def _is_own_domain(sender: str) -> bool:
    """Vérifie si l'expéditeur appartient à un des domaines de Detective.be."""
    own_domains = ("detectivebelgique.be", "detectivebelgium.com", "dpdhuinvestigations.be")
    return any(d in sender.lower() for d in own_domains)


def _is_wp_contact_form(body: str) -> bool:
    """Détecte un formulaire de contact WordPress (detectivebelgium.com NL ou
    detectivebelgique.be FR). Ces mails arrivent via un expéditeur technique
    (mail@/wordpress@/contact@detective*) avec un sujet parfois trompeur, mais
    le body structuré en champs est la signature fiable d'une vraie demande client.
    """
    b = body.lower()
    nl_hits = sum(1 for f in _WP_FORM_NL_FIELDS if f in b)
    if nl_hits >= 2 and _WP_FORM_NL_MARKER in b:
        return True
    fr_hits = sum(1 for f in _WP_FORM_FR_FIELDS if f in b)
    if fr_hits >= 2 and _WP_FORM_FR_MARKER in b:
        return True
    # Cas robuste : 3 champs NL sans le marker (formulaire variant)
    if nl_hits >= 3:
        return True
    return False


def _get_body_text(msg: Message, max_chars: int = 8000) -> str:
    """Extrait le corps texte complet (jusqu'à max_chars) pour analyse."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")[:max_chars]
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    text = re.sub(r"<[^>]+>", " ", payload.decode("utf-8", errors="replace"))
                    return text[:max_chars]
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            return text[:max_chars]
    return ""


# Domaines d'outils métiers connus (compta, banque, logiciels internes)
_KNOWN_LEGIT_DOMAINS = (
    "mailer.falco-app.be",   # Logiciel de comptabilité FALCO
)


def _is_known_legit_sender(sender: str) -> bool:
    return any(d in sender.lower() for d in _KNOWN_LEGIT_DOMAINS)


def is_phishing(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)
    sender = (msg.get("From", "") or "").lower()

    # Exception 1 : formulaires de contact du propre site
    if any(fs in subject for fs in FORM_SUBJECTS):
        return False

    # Exception 2 : mails auto-générés par le propre domaine
    if _is_own_domain(sender):
        return False

    # Exception 3 : outils métiers connus (FALCO compta, etc.)
    if _is_known_legit_sender(sender):
        return False

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
    return bool(_has_attachment(msg))


def is_service_email(msg: Message) -> bool:
    """Email automatique d'un service/fournisseur : renouvellement, confirmation, alerte..."""
    # Garde : si c'est une newsletter (headers évidents), ne PAS classer comme service
    if is_newsletter(msg):
        return False

    sender = (msg.get("From", "") or "").lower()
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)

    # Expéditeur connu de service
    if any(s in sender for s in SERVICE_SENDERS):
        return True
    # Mots-clés automatiques dans le sujet (forts)
    if any(kw in subject for kw in AUTRE_KEYWORDS):
        return True
    # Mots-clés automatiques dans le corps
    if any(kw in body_snippet for kw in AUTRE_KEYWORDS):
        return True
    # Headers typiques des emails automatiques
    return bool(msg.get("Auto-Submitted") or msg.get("X-Auto-Response-Suppress"))


def is_autre(msg: Message) -> bool:
    """Notifications automatiques, invitations calendrier, etc."""
    return is_service_email(msg)


def is_rappel(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)

    if any(kw in subject for kw in RAPPEL_KEYWORDS):
        return True
    return bool(any(kw in body_snippet for kw in RAPPEL_KEYWORDS))


def is_facture(msg: Message) -> bool:
    subject = (msg.get("Subject", "") or "").lower()
    body_snippet = _get_body_snippet(msg)
    sender = (msg.get("From", "") or "").lower()

    if any(kw in subject for kw in FACTURE_KEYWORDS):
        return True
    if any(kw in body_snippet for kw in FACTURE_KEYWORDS):
        return True
    return bool(any(s in sender for s in FACTURE_SENDERS))


def is_demande_client(msg: Message) -> bool:
    """Détection EXTRÊMEMENT conservative d'une demande client.
    Renvoie True UNIQUEMENT pour les formulaires de contact du propre site.
    TOUT le reste passe par le LLM classifier."""
    subject = (msg.get("Subject", "") or "").lower()
    sender = (msg.get("From", "") or "").lower()

    # Jamais un email de service automatique
    if is_service_email(msg):
        return False
    # Jamais un email du propre domaine (auto-notifications internes)
    if _is_own_domain(sender):
        return False

    # UNIQUEMENT les formulaires de contact du site web
    return bool(any(fs in subject for fs in FORM_SUBJECTS))


def is_wordpress_contact_form(msg: Message) -> bool:
    """True si le body contient les champs structurés d'un formulaire WP
    detectivebelgium.com (NL) ou detectivebelgique.be (FR).
    """
    return _is_wp_contact_form(_get_body_text(msg, max_chars=8000))


def quick_classify(msg: Message) -> str | None:
    """Retourne une catégorie si une règle ÉVIDENTE s'applique, sinon None
    (→ on délègue au LLM classifier qui a un vrai cerveau).

    ORDRE : du plus spécifique/dangereux au plus général.
    WordPress contact form EN PREMIER — c'est une demande_client incontestable
    et son sujet/body peuvent matcher newsletter/service/facture à tort.
    Newsletter AVANT service_email car les newsletters (SendGrid, etc.)
    peuvent matcher des mots-clés service dans leur corps.
    demande_client est EXCLU du pré-filtre rapide (trop de faux positifs),
    sauf les formulaires WordPress du propre site.
    """
    if is_wordpress_contact_form(msg):
        return "demande_client"
    if is_phishing(msg):
        return "phishing"
    if is_newsletter(msg):
        return "newsletter"
    if is_service_email(msg):
        return "autre"
    if is_facture(msg):
        return "facture"
    if is_rappel(msg):
        return "rappel"
    # Extrêmement conservatif : uniquement formulaires de contact du site
    if is_demande_client(msg):
        return "demande_client"
    return None
