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
    "c'est combien",
    "c combien",
    "quel est le tarif",
    "quel est le prix",
    "quels sont vos tarifs",
    "vos tarifs",
    "votre prix",
    "votre tarif",
    "tarif exact",
    "prix exact",
    "devis",
    "combien ça coûte",
    "combien cela coûte",
    "combien coûte",
    "je vous contacte",
    "je vous écris",
    "je vous sollicite",
    "je souhaite",
    "je voudrais",
    "je cherche",
    "je désire",
    "j'aimerais",
    "est-ce que vous",
    "pouvez-vous",
    "pourriez-vous",
    "merci de me",
    "merci de bien vouloir",
    "dans l'attente",
    "en attente de votre",
    "j'attends de vos",
    "j'attends votre",
    "merci de votre retour",
    "merci de votre réponse",
    "pourriez-vous m'",
    "pouvez-vous m'",
    "avez-vous eu",
    "avez-vous bien reçu",
    "suivi de ma demande",
    "faire suite à",
    "fais suite à",
    "suite à votre",
    "suite à ma",
    "relance",
    "relancer",
    "nouvelles de",
    "des nouvelles",
    "avez-vous",
    "as-tu",
    "puis-je",
    "est-il possible",
    # Équivalents NL (néerlandais) — Detective Belgium
    "wens ik",
    "wenst u",
    "wilt u",
    "zou u",
    "kan u",
    "kunt u",
    "offerte",
    "prijs",
    "kost",
    "kosten",
    "tarief",
    "ik wil",
    "ik zou",
    "ik had",
    "ik heb",
    "alvast bedankt",
    "met vriendelijke groet",
    "mvg",
    "vraag",
    "vraagje",
    # Équivalents EN (anglais) — multilingue
    "i would like",
    "i want",
    "i need",
    "how much",
    "what is the cost",
    "could you",
    "would you",
    "please send",
    "quote",
    "i'm looking",
    "looking for",
    "kind regards",
    "regards",
    "best regards",
    # Questions à la 1ère personne sur l'enquête (typique Detective.be)
    "ma femme",
    "mon mari",
    "ma conjointe",
    "mon conjoint",
    "mon partenaire",
    "mon entreprise",
    "ma société",
    "mon employé",
    "ma salariée",
    "suspecte",
    "soupçonne",
    "soupçonn",
    "douter",
    "doute",
    "infidèle",
    "infidélité",
    "tromper",
    "adultère",
    "filature",
    "surveillance",
    "enquête",
    "investigation",
    "enquete",
    "détective",
    "detective",
    "privé",
    "mijn vrouw",
    "mijn man",
    "mijn partner",  # NL
    "my wife",
    "my husband",
    "my partner",  # EN
    # Verbes d'action métier
    "vérifier",
    "controler",
    "contrôler",
    "découvrir",
    "prouver",
    "confirmer",
    "identifier",
    "retrouver",
    "localiser",
)

_HUMAN_HINTS_IN_BODY = (
    # Présence d'un prénom/nom en début de mail
    "bonjour",
    "bonsoir",
    "hello",
    "hi ",
    "cher monsieur",
    "chère madame",
    "cher m.",
    "chère mme",
    "geachte",
    "beste",
    "beste heer",
    "beste mevrouw",
    "monsieur hurchon",
    "madame hurchon",
    "mr hurchon",
    "mme hurchon",
    "m. hurchon",
)

# v1.24.0 — détection des formulaires de contact WordPress (toutes boîtes).
# Les sites detectivebelgium.com (NL) et detectivebelgique.be (FR) transfèrent
# les demandes du formulaire web vers la boîte mail avec un expéditeur technique
# (mail@/wordpress@/contact@detective*) et un sujet parfois trompeur (template WP
# « Réinitialisation du mot de passe »). Le body structuré en champs est la seule
# signature fiable. Voir mails #515 (Nathalie Hairemans), #555, #503, #615.
_WP_FORM_NL_FIELDS = ("achternaam:", "voornaam:", "telefoonnummer:")
_WP_FORM_FR_FIELDS = ("nom:", "prénom:", "téléphone:", "prenom:", "telephone:")
_WP_FORM_FR_MARKER = "votre profil"
_WP_FORM_NL_MARKER = "hoe kunnen wij u helpen"

# v1.24.0 — marqueurs d'un phishing ACTIF dans le body. Si présents, on ne remonte
# JAMAIS depuis phishing même si le body ressemble à une demande. Permet de
# distinguer #614 (demande réelle) d'un vrai phishing. On ne met PAS les URL
# (https://) ici : un client peut légitimement mentionner un profil Facebook, un
# site, etc. On cible uniquement les CTAs d'urgence de phishing.
_ACTIVE_PHISHING_MARKERS = (
    "cliquez ici",
    "cliquez sur le lien",
    "click here",
    "click the link",
    "cliquez pour vérifier",
    "vérifiez votre identité",
    "verify your identity",
    "confirmez votre compte",
    "votre compte a été suspendu",
    "your account has been suspended",
    "votre compte sera désactivé",
    "reactiver votre compte",
    "réactiver votre compte",
    "action requise sur votre compte",
    "votre compte sera bloqué",
    "connexion à votre espace",
    "identifiants sont erronés",
)

# v1.24.0 — vocabulaire métier enquête (FR + NL + EN). Présence combinée à une
# signature prénom + question de tarif = demande humaine forte (exception phishing).
_ENQUIRY_VOCAB = (
    "infidélité",
    "infidélite",
    "infidele",
    "infidèle",
    "adultère",
    "tromper",
    "trompe",
    "filature",
    "surveillance",
    "enquête",
    "enquete",
    "investigation",
    "détective",
    "detective",
    "soupçonn",
    "suspect",
    "prouver",
    "preuve",
    "attraper la main dans le sac",
    "attraper",
    "mandant",
    "mission",
    "incapacité",
    "incapacite",
    "arrêt maladie",
    "arret maladie",
    "maladie",
    "ontrouw",
    "overspel",
    "afluisteren",
    "bewaking",
    "onderzoek",
    "privédetective",
    "privedetective",
    "detective",
    "cheating",
    "affair",
    "surveillance",
    "investigation",
    "private detective",
)

# v1.24.0 — question de tarif (FR + NL + EN). Indique un prospect réel.
_PRICE_QUESTION = (
    "combien",
    "tarif",
    "coûte",
    "coute",
    "prix",
    "devis",
    "estimation",
    "offerte",
    "prijs",
    "kosten",
    "tarief",
    "hoeveel",
    "how much",
    "cost",
    "price",
    "quote",
    "fee",
)

# Signature prénom en fin de body : "merci\nSerge M" / "Bien à vous,\nFrédéric Van Houtte"
# / "Groeten,\nToon Breyne". On cherche une ligne finale avec 1-3 mots dont le 1er
# commence par une majuscule (prénom). Pas trop strict pour tolérer les fautes.
_SIGNED_NAME_RE = re.compile(
    r"(?:merci|groet(?:en)?|mvg|met vriendelijke groet|bien à vous|bien a vous|cordialement|regards|best regards)\s*[,.\s]*\n*\s*([A-ZÀ-Ý][\wà-ÿ\-]+(?:\s+[A-ZÀ-Ý\w][\wà-ÿ\-]*){0,3})\s*$",
    re.IGNORECASE,
)
# Fallback : juste un prénom + nom en toute fin de body (sans formule de politesse).
_TRAILING_NAME_RE = re.compile(r"\n\s*([A-ZÀ-Ý][\wà-ÿ\-]+(?:\s+[A-ZÀ-Ý\w][\wà-ÿ\-]*){0,3})\s*$")


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


def _has_strong_human_demand(body: str) -> tuple[bool, str]:
    """Détecte une demande humaine forte dans le body : prénom signé + vocabulaire
    métier enquête + question de tarif, SANS marqueur de phishing actif.

    Utilisé pour autoriser la remontée depuis phishing/spam (sinon interdit).
    Retourne (match, raison).
    """
    b = body.lower()
    if any(m in b for m in _ACTIVE_PHISHING_MARKERS):
        return False, "active_phishing_marker"
    has_vocab = any(v in b for v in _ENQUIRY_VOCAB)
    has_price = any(p in b for p in _PRICE_QUESTION)
    if not (has_vocab and has_price):
        return False, "no_enquiry_or_price"
    # Signature prénom : formule de politesse + nom, OU nom en fin de body.
    signed = bool(_SIGNED_NAME_RE.search(body.strip()))
    if not signed:
        # Fallback : prénom en fin de body (ex "merci\nSerge M")
        tail = body.strip()[-120:]
        signed = bool(_TRAILING_NAME_RE.search(tail))
    if not signed:
        return False, "no_signed_name"
    return True, "strong_human_demand"


# v1.24.0 — détection d'une réponse client à un mail de Daniel (Re: + citation).
# Le body cite un mail de Daniel (préfixe > avec signature DetectiveBelgique/
# DetectiveBelgium/DPDH). L'expéditeur est humain. On force demande_client même
# si le LLM voit des mots "devis/facture" dans la citation. Voir mail #606 (Van Houtte).
_DANIEL_SIGNATURE_PATTERNS = (
    "daniel hurchon",
    "detectivebelgique.be srl",
    "detectivebelgium.com",
    "détectivebelgique.be srl",
    "detectivebelgique.be srl",
    "autorisation ministérielle",
    "autorisation ministeriele",
    "gsm – 0471/31.81.20",
    "gsm - 0471/31.81.20",
    "chaussée bara 213",
    "chaussÃ©e bara 213",
)


# v1.25.8 — réponse/relance humaine sans citation Daniel (#Vacature).
# Préfixes de réponse multilingues (FR/NL/EN/DE).
_FOLLOWUP_SUBJECT_PREFIXES_RE = re.compile(
    r"^(?:re|rép|rép|aw|antw|sv|wtr|fwd|fw|doorsturen)\s*[:\.\s]",
    re.IGNORECASE,
)

# Marqueurs de relance / suivi de demande (FR + NL + EN + DE).
_FOLLOWUP_RELANCE_MARKERS = (
    # FR
    "avez-vous reçu",
    "avez vous reçu",
    "as-tu reçu",
    "as tu reçu",
    "avez-vous bien reçu",
    "avez vous bien reçu",
    "avez vous mon",
    "est-ce que vous avez reçu",
    "est ce que vous avez reçu",
    "suivi de ma demande",
    "faire suite",
    "fais suite",
    "suite à mon",
    "suite à ma demande",
    "relance",
    "des nouvelles",
    "nouvelles de",
    "pas de nouvelles",
    "donner des nouvelles",
    "pourriez-vous me tenir",
    "pourriez vous me tenir",
    "pouvez-vous me tenir",
    "pouvez vous me tenir",
    "en attente de votre",
    "j'attends votre",
    "j'attends de vos",
    # NL
    "heeft u ontvangen",
    "heeft u mijn",
    "heb je ontvangen",
    "heb je mijn",
    "is mijn e-mail aangekomen",
    "is mijn mail aangekomen",
    "opvolging",
    "vervolg",
    "vervolg op",
    "nieuws over",
    "nieuws van",
    "geen nieuws",
    "herinnering",
    "terugkomend op",
    "wacht op",
    "in afwachting van",
    "blijf in afwachting",
    # EN
    "did you receive",
    "have you received",
    "have you got",
    "follow up",
    "following up",
    "any update",
    "any news",
    "news about",
    "just checking",
    "checking in",
    # DE
    "haben sie erhalten",
    "haben sie meine",
    "nachfrage",
    "rückfrage",
)

# v1.25.8 — candidature/spontanée (NL vacature / FR candidature). Certains job-boards
# envoient depuis des senders marketing et sont classés newsletter à tort.
_JOB_APPLICATION_MARKERS = (
    "vacature",
    "candidature",
    "sollicitatie",
    "job",
    "emploi",
    "stagiair",
    "stage",
    "zelfstandige",
    "bijberoep",
    "bijverdienste",
    "recherche",
)

# Senders de service / marketing dont une relance ne peut PAS être humaine.
_FOLLOWUP_SERVICE_SENDERS = (
    "noreply",
    "no-reply",
    "ne-pas-repondre",
    "donotreply",
    "mailer-daemon",
    "wordpress@",
    "mail@detective",
    "contact@detective",
    "newsletter@",
    "promo@",
    "marketing@",
    "campaign@",
    "mailing@",
    "info@arval",
    "info@",  # domaine marketing générique ; garder strict
)


def _is_reply_to_daniel(body: str, sender: str) -> bool:
    """Re: + body cite un mail de Daniel (préfixe > + signature cabinet) +
    expéditeur humain (pas un service/no-reply)."""
    sender_l = sender.lower()
    service_hints = (
        "noreply",
        "no-reply",
        "ne-pas-repondre",
        "mailer-daemon",
        "infomaniak",
        "wordpress@",
        "mail@detective",
        "contact@detective",
    )
    # mail@/contact@detective* = forwarder formulaire (pas une réponse humaine à Daniel)
    if any(h in sender_l for h in service_hints):
        return False
    if ">" not in body:
        return False
    b = body.lower()
    return any(sig in b for sig in _DANIEL_SIGNATURE_PATTERNS)


def _is_human_followup(subject: str, body: str, sender: str) -> bool:
    """Relance/suivi d'un humain (Re:/Rép.:/AW:/Wtr. + marqueur de relance +
    expéditeur humain). Indépendant de la citation Daniel — certains clients
    relancent sans citer le mail initial (#Vacature)."""
    sender_l = sender.lower().strip()
    if not sender_l:
        return False
    service_senders = (
        "noreply",
        "no-reply",
        "ne-pas-repondre",
        "donotreply",
        "mailer-daemon",
        "infomaniak",
        "wordpress@",
        "mail@detective",
        "contact@detective",
        "newsletter@",
        "promo@",
        "marketing@",
        "campaign@",
        "mailing@",
        "info@arval",  # marketing connu
    )
    if any(s in sender_l for s in service_senders):
        return False

    text = f"{subject}\n{body}".lower()
    has_relance_marker = any(m in text for m in _FOLLOWUP_RELANCE_MARKERS)
    has_reply_prefix = bool(_FOLLOWUP_SUBJECT_PREFIXES_RE.search(subject))
    body_stripped = body.strip()
    is_signed = bool(_SIGNED_NAME_RE.search(body_stripped)) or bool(
        _TRAILING_NAME_RE.search(body_stripped[-120:])
    )

    # Cas fort : préfixe Re: + marqueur de relance explicite.
    if has_reply_prefix and has_relance_marker:
        return True
    # Cas modéré : préfixe Re: + body signé + vocabulaire de suivi/question.
    followup_words = (
        "merci",
        "mvg",
        "cordialement",
        "groeten",
        "regards",
        "vragen",
        "vraag",
        "question",
    )
    return has_reply_prefix and is_signed and any(q in text for q in followup_words)


def _is_job_application(subject: str, body: str, sender: str) -> bool:
    """Candidature spontanée / demande d'emploi. Les job-boards peuvent forwarder
    depuis des senders marketing et être classés newsletter à tort."""
    sender_l = sender.lower().strip()
    if not sender_l:
        return False
    service_senders = (
        "noreply",
        "no-reply",
        "ne-pas-repondre",
        "donotreply",
        "mailer-daemon",
        "newsletter@",
        "promo@",
        "marketing@",
        "campaign@",
        "mailing@",
    )
    if any(s in sender_l for s in service_senders):
        return False

    text = f"{subject}\n{body}".lower()
    has_job_marker = any(m in text for m in _JOB_APPLICATION_MARKERS)
    body_stripped = body.strip()
    is_signed = bool(_SIGNED_NAME_RE.search(body_stripped)) or bool(
        _TRAILING_NAME_RE.search(body_stripped[-120:])
    )

    # Candidature : sujet/body job + signé.
    return has_job_marker and is_signed


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
        "noreply",
        "no-reply",
        "ne-pas-repondre",
        "donotreply",
        "infomaniak",
        "microsoft",
        "google",
        "apple",
        "meta",
        "facebook",
        "linkedin",
        "twitter",
        "x.com",
        "github",
        "gitlab",
        "atlassian",
        "stripe",
        "paypal",
        "ovh",
        "sendgrid",
        "mailgun",
        "brevo",
        "mailchimp",
        "hubspot",
        "zendesk",
        "intercom",
        "freshdesk",
        "support@",
        "billing@",
        "invoice@",
        "facture@",
        "compta@",
        "accounting@",
        "newsletter@",
        "promo@",
        "marketing@",
        # Expéditeurs de plateformes publicitaires / corporate connues.
        "ads-google",
        "googleads",
        "google-ads",
        "bauermedia",
        "outdoor.com",
    )
    if any(s in sender_l for s in service_senders):
        return False

    # 2. mots-clés de service évidents dans le sujet
    service_subjects = (
        "facture",
        "invoice",
        "recu",
        "reçu",
        "payment",
        "paiement",
        "subscription",
        "abonnement",
        "renewal",
        "renouvellement",
        "validation en deux étapes",
        "2-step",
        "two-step",
        "alerte de sécurité",
        "security alert",
        "new sign-in",
        "nouvelle connexion",
        "votre identifiant a été utilisé",
        "votre compte",
        "rapport de maintenance",
        "votre reçu",
        "your receipt",
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
    """v1.22.1 — post-traitement non-négociable. v1.24.0 — élargi aux formulaires WP,
    aux réponses à Daniel et aux demandes humaines fortes (exception phishing).

    Règle absolue CDAL : on ne rate AUCUN demande_client. Faux positifs acceptables
    (max 1-2%), faux négatifs intolérables (Daniel perd des clients).

    Ordre de priorité (du plus sûr au plus risqué) :
    1. Formulaire de contact WordPress (body structuré) → force depuis TOUTE catégorie.
       Un formulaire WP est toujours une vraie demande client, jamais un phishing.
    2. Réponse client à un mail de Daniel (Re: + citation signée) → force depuis TOUTE
       catégorie (expéditeur humain déjà vérifié).
    3. Relance/suivi humain (Re:/Rép.:/AW: + marqueur de relance) → remontée depuis
       autre/facture/rappel/urgent/newsletter (#Vacature : pas de citation Daniel).
    4. Candidature spontanée → remontée depuis autre/newsletter.
    5. Demande humaine forte (prénom signé + vocabulaire enquête + question tarif, sans
       marqueur phishing actif) → autorise la remontée depuis phishing/spam/newsletter.
    6. Heuristique humaine classique → remontée depuis autre/facture/rappel/urgent.
    """
    if llm_category == "demande_client":
        return "demande_client"

    # 1. Formulaire WordPress — signature body, sans ambiguïté.
    if _is_wp_contact_form(body):
        log.info(
            "classifier.recall_override",
            rule="wp_contact_form",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    # 2. Réponse client à un mail de Daniel (Re: + citation signée).
    if _is_reply_to_daniel(body, sender):
        log.info(
            "classifier.recall_override",
            rule="reply_to_daniel",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    # 3. Relance/suivi humain sans citation Daniel (#Vacature) — peut remonter
    # depuis autre/facture/rappel/urgent/newsletter. Jamais depuis spam/phishing.
    followup_upgradable = {"autre", "facture", "rappel", "urgent", "newsletter"}
    if llm_category in followup_upgradable and _is_human_followup(subject, body, sender):
        log.info(
            "classifier.recall_override",
            rule="human_followup",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    # 4. Candidature spontanée / demande d'emploi — peut venir d'un job-board
    # classé newsletter/autre à tort.
    job_upgradable = {"autre", "newsletter"}
    if llm_category in job_upgradable and _is_job_application(subject, body, sender):
        log.info(
            "classifier.recall_override",
            rule="job_application",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    # 5. Demande humaine forte — autorise la remontée depuis phishing/spam/newsletter.
    strong, _reason = _has_strong_human_demand(body)
    if strong:
        log.info(
            "classifier.recall_override",
            rule="strong_human_demand",
            llm_said=llm_category,
            forced_to="demande_client",
            subject=subject[:60],
            sender=sender[:40],
        )
        return "demande_client"

    # 6. Comportement v1.22.1 : remontée classique depuis autre/facture/rappel/urgent.
    upgradable = {"autre", "facture", "rappel", "urgent"}
    if llm_category not in upgradable:
        return llm_category  # type: ignore[return-value]

    if _looks_like_human_question(body, subject, sender):
        log.info(
            "classifier.recall_override",
            rule="human_question",
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
        # v1.25.0 : 15 → 200. gemma4:31b (principal) répond en 1 mot et s'arrête
        # naturellement (max_tokens = plafond, pas cible). Mais le fallback
        # glm-5.2:cloud est un reasoning model : il consomme d'abord des tokens
        # en raisonnement avant la réponse. Avec 15 tokens, le fallback ne
        # pouvait jamais répondre → mail classé "autre" silencieusement
        # (faux négatif). 200 laisse la place au raisonnement + à la catégorie.
        max_tokens=200,
        temperature=0.0,
    )
    raw = response.strip().lower().split()[0] if response.strip() else "autre"
    if raw not in VALID_CATEGORIES:
        log.warning("classifier.invalid_response", raw=raw)
        return _enforce_recall_over_precision("autre", subject, body, sender)

    # v1.22.1 — post-traitement qui force demande_client en cas de doute
    return _enforce_recall_over_precision(raw, subject, body, sender)  # type: ignore[arg-type]
