import asyncio
import contextlib
import hashlib
import html
import re
import sqlite3
from datetime import datetime, timedelta
from email import header, message_from_bytes
from email.errors import HeaderParseError
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import structlog
from aioimaplib import aioimaplib

from app.alerts import alert_imap_draft_failure
from app.cerveau_client import feed_correspondance, feed_document
from app.cerveau_dossier import derive_dossier_id
from app.config import MailboxConfig, get_settings
from app.delivery.imap_draft import append_draft
from app.delivery.resend_notifier import IncomingMail, notify_draft
from app.delivery.slack_notifier import notify_new_draft as notify_slack_draft
from app.healthcheck import health
from app.pipeline.classifier import _is_human_followup, _is_reply_to_daniel, classify
from app.pipeline.document_extract import extract_text_bytes, is_supported
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language
from app.pipeline.prefilter import quick_classify
from app.pipeline.priority import assign_priority
from app.pipeline.subject_fixer import fix_subject_llm, is_subject_suspect, tag_no_email

# Signaux de coordonnées contact dans un body (tél belge, code postal, adresse)
_CONTACT_SIGNALS_RE = re.compile(
    r"(?:"
    r"0\d[\d\s/\.\-]{6,12}|"  # mobile belge 04xx/...
    r"\+32[\s\d]{8,15}|"  # +32 ...
    r"\b\d{4}\s+[A-ZÀ-Ÿa-z][a-zà-ÿ]|"  # code postal + ville
    r"(?:Rue|Avenue|Boulevard|Chaussée|Place|Drève|Chemin|Av\.)\s"
    r")",
    re.IGNORECASE,
)

# Mapping mailbox.name → marque Cerveau2
_MARQUE_CERVEAU2 = {
    "detective_belgique": "detectivebelgique",
    "detective_belgium": "detectivebelgium",
    "dpdh_investigations": "dpdhu",
}

log = structlog.get_logger()

AGENT_FLAG = "AgentProcessed"
# v1.21.3 : flag posé après crash d'un mail pour libérer la queue IMAP
# (le mail ne sera pas rejoué indéfiniment, et il n'est pas non plus classé comme traité).
AGENT_ATTEMPTED_FLAG = "AgentAttempted"
IMAP_RETRY_ATTEMPTS = 3

# Emails système auto-générés à ignorer (ne pas insérer en DB)
_SYSTEM_SENDERS = ("noreply@resend.digitalhs.biz",)

# Expéditeurs qui ne peuvent JAMAIS être un vrai client
_SERVICE_SENDERS = (
    "infomaniak",
    "ovh",
    "stripe",
    "paypal",
    "amazon",
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
    "sendgrid",
    "mailgun",
    "brevo",
    "mailchimp",
    "hubspot",
    "zendesk",
    "intercom",
    "freshdesk",
)

# Marqueurs indiquant qu'un mail est une réponse d'un client à un précédent échange
_FOLLOWUP_SUBJECT_RE = re.compile(r"^re\s*:\s*", re.IGNORECASE)
_FOLLOWUP_BODY_MARKERS = (
    "voici",
    "en réponse à",
    "comme demandé",
    "comme convenu",
    "cf. ci-joint",
    "vous trouverez ci-joint",
    "re-bonjour",
    "pour compléter",
    "suite à",
    "ci-joint",
    "merci de bien vouloir",
    "je vous transmets",
    "je vous envoie",
    "je joins",
    "en pièce jointe",
    "pj",
    "piece jointe",
    "ci-dessous",
)


def _decode_header(value: str) -> str:
    """Décode un header MIME RFC 2047 (ex: =?UTF-8?Q?...?=).

    v1.21.3 : chaîne de fallback charset → utf-8 → latin-1 → replace pour absorber
    les clients mail exotiques qui émettent 'unknown-8bit' (cause du bug prod).
    """
    if not value:
        return ""
    try:
        decoded_parts = header.decode_header(value)
    except (HeaderParseError, ValueError):
        return str(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            decoded = False
            for enc in (charset, "utf-8", "latin-1"):
                if not enc:
                    continue
                try:
                    result.append(part.decode(enc, errors="strict"))
                    decoded = True
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            if not decoded:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _get_body_text(msg: Message) -> str:
    """Extraire le texte plain d'un email, ou HTML détaggé en fallback.

    Certains formulaires web envoient un text/plain incomplet (champs
    manquants) avec un text/html complet. On concatène toutes les parties
    inline, et on fallback sur HTML si celui-ci est significativement plus riche.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            # Ignorer les pièces jointes explicites
            if disposition and disposition.lower().startswith("attachment"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            decoded = payload.decode("utf-8", errors="replace")
            if content_type == "text/plain":
                text_parts.append(decoded)
            elif content_type == "text/html":
                html_parts.append(html.unescape(re.sub(r"<[^>]+>", "", decoded)))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(html.unescape(re.sub(r"<[^>]+>", "", decoded)))
            else:
                text_parts.append(decoded)

    body_plain = "\n".join(text_parts).strip()
    body_html = "\n".join(html_parts).strip()

    # Fallback HTML si le text/plain est anormalement court (formulaire WP incomplet)
    if len(body_plain) < 200 and len(body_html) > len(body_plain) * 2:
        return body_html
    return body_plain if body_plain else body_html


def _log_telemetry(
    db_path: Path,
    event_type: str,
    mailbox_name: str | None,
    details: str,
) -> None:
    """Écrit un événement de télémétrie dans agent_state.db (agent_telemetry)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agent_telemetry (event_type, mailbox_name, details) VALUES (?, ?, ?)",
            (event_type, mailbox_name, details),
        )
        conn.commit()
    finally:
        conn.close()


def _log_audit(
    db_path: Path,
    mailbox_name: str,
    cycle_result: str,
    details: str,
) -> None:
    """Écrit un event dans audit_logs à chaque fin de cycle de polling (v1.21.7).

    But : traçabilité client — Daniel peut voir dans /audit que Charlie a bien
    tourné sur chaque boîte, même quand 0 mail n'est trouvé. Sans ce log, un
    silence prolongé ressemble à un crash. Avec, on a la preuve quotidienne
    que le service est actif.

    `cycle_result` ∈ {ok, empty, error} — utile pour filtrer rapidement.
    Best-effort : si l'INSERT échoue (DB lock, schema manquant), on log et
    on continue. Le poller ne doit JAMAIS crasher pour une raison d'audit.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO audit_logs (
                    user_id, action, resource_type, resource_id, details,
                    ip_address, user_agent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    None,
                    "poller.cycle",
                    "mailbox",
                    mailbox_name,
                    f"{cycle_result} | {details}",
                    None,
                    "charlie-poller",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("poller.audit_log_failed", mailbox=mailbox_name, error=str(exc))


def _mail_exists(db_path: Path, imap_uid: str, mailbox_name: str) -> bool:
    """Vérifie si un mail a déjà été persisté (pour éviter re-génération/re-notification)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM mail_processed WHERE imap_uid = ? AND mailbox_name = ?",
            (imap_uid, mailbox_name),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _is_known_sender(db_path: Path, sender: str) -> bool:
    """Vérifie si l'expéditeur a déjà envoyé des emails traités.

    Garde anti-faux-positif phishing : un expéditeur connu ne devrait
    pas être classé phishing automatiquement par le prefilter.
    """
    sender_norm = sender.lower().strip()
    if not sender_norm:
        return False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT 1 FROM mail_processed WHERE LOWER(sender) = ? LIMIT 1",
            (sender_norm,),
        )
        found = bool(cursor.fetchone())
        conn.close()
        return found
    except Exception:
        return False


def _is_client_followup(db_path: Path, sender: str, msg: Message) -> bool:
    """Détecte si le mail est une réponse d'un client à un échange récent.

    Conditions :
    - le mail ressemble à une réponse (header In-Reply-To/References, sujet Re:,
      ou marqueurs body type 'voici', 'ci-joint', 'en réponse à') ;
    - l'expéditeur a déjà envoyé un mail classé 'demande_client' dans les 30
      derniers jours (selon la date du header Date, RFC 2822).

    Si les deux sont vrais, on génère un brouillon court de remerciement au lieu
    du brouillon qualifiant standard.
    """
    sender_norm = sender.lower().strip()
    if not sender_norm:
        return False

    body = _get_body_text(msg)

    # v1.25.7 — shortcut : Re: + citation d'un mail de Daniel (préfixe > + signature
    # cabinet) = réponse à un échange existant. Preuve indépendante de l'historique
    # DB (qui peut manquer si le mail initial a été traité hors-agent / autre boîte).
    # Cf. #606 (Van Houtte) : pas d'historique DB mais citation explicite d'un mail
    # de Daniel du 16 juin → brouillon ack au lieu du qualifiant qui redemandait
    # nom/prénom comme un nouveau prospect.
    if _is_reply_to_daniel(body, sender):
        return True

    # v1.25.8 — relance/accusé de réception humain sans citation Daniel (#Vacature).
    # Le client relance simplement (« avez-vous bien reçu mon email ? ») ; c'est
    # un follow-up au sens métier même sans historique DB.
    subject = str(msg.get("Subject", "") or "")
    if _is_human_followup(subject, body, sender):
        return True

    # --- 1. Le mail ressemble-t-il à une réponse ? ---
    is_reply = bool(msg.get("In-Reply-To") or msg.get("References"))
    if _FOLLOWUP_SUBJECT_RE.search(subject):
        is_reply = True

    body_l = body.lower()
    if any(marker in body_l for marker in _FOLLOWUP_BODY_MARKERS):
        is_reply = True

    if not is_reply:
        return False

    # --- 2. L'expéditeur a-t-il un dossier récent (30j) ? ---
    # La date du header est en RFC 2822 : on filtre en Python pour être fiable.
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT id, received_at
            FROM mail_processed
            WHERE LOWER(sender) = ? AND category = 'demande_client'
            ORDER BY id DESC
            LIMIT 50
            """,
            (sender_norm,),
        ).fetchall()
        conn.close()
    except Exception:
        return False

    if not rows:
        return False

    cutoff = datetime.now().astimezone() - timedelta(days=30)
    rfc_re = re.compile(r"[A-Za-z]{3},\s+(\d+)\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    for _mid, received_at in rows:
        received_at = received_at or ""
        m = rfc_re.match(str(received_at))
        if not m:
            continue
        day, mon_s, year, hh, mm, ss = m.groups()
        try:
            dt = datetime(
                int(year),
                months[mon_s],
                int(day),
                int(hh),
                int(mm),
                int(ss),
                tzinfo=None,
            )
        except (KeyError, ValueError):
            continue
        # received_at est en UTC (RFC 2822 utilise +0000 par défaut dans notre flux),
        # cutoff est local → on compare en ignorant le tz (marge de quelques heures OK).
        if dt.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
            return True

    return False


def _is_system_email(sender: str) -> bool:
    """Emails auto-générés (magic links, notifs système) à ignorer."""
    sender_norm = sender.lower().strip()
    return any(s in sender_norm for s in _SYSTEM_SENDERS)


def _normalize_contact_key(
    email: str | None, tel: str | None, nom: str, dossier_id: str | None
) -> str:
    """Clé de dédup stable : email > tel > nom+dossier."""
    import unicodedata

    def strip_accents(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
        )

    if email:
        return f"contact-{hashlib.md5(email.lower().strip().encode(), usedforsecurity=False).hexdigest()[:12]}"
    if tel:
        tel_clean = re.sub(r"[\s/\.\-]", "", tel)
        return f"contact-{hashlib.md5(tel_clean.encode(), usedforsecurity=False).hexdigest()[:12]}"
    nom_clean = strip_accents(nom.lower().strip()) if nom else "inconnu"
    key = f"{nom_clean}:{dossier_id or 'global'}"
    return f"contact-{hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:12]}"


# ── Regex extraction entreprise (pas de LLM, instantané) ──
_FORMES_JURIDIQUES_RE = re.compile(
    r"\b(?:SA|S\.A\.|SRL|S\.R\.L\.|BVBA|B\.V\.B\.A\.|SPRL|S\.P\.R\.L\.|ASBL|A\.S\.B\.L\.|"
    r"SCS|S\.C\.S\.|SCA|S\.C\.A\.|SCRL|S\.C\.R\.L\.|NV|N\.V\.|VBA|V\.B\.A\.|GIE|G\.I\.E\.|SE)\b",
    re.IGNORECASE,
)
_ENTREPRISE_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9\s\&\.\-]{1,40}?(?:\s+(?:SA|S\.A\.|SRL|S\.R\.L\.|BVBA|SPRL|NV|SCRL|ASBL|SCS|"
    r"SCA|SE|VBA|GIE|Group|GROUP|International|Belgium|Europe)))\b",
)
_TVA_BE_RE = re.compile(r"\bBE\s*0?\d{3}[\.\s]?\d{3}[\.\s]?\d{3}\b", re.IGNORECASE)
_ADRESSE_BE_RE = re.compile(
    r"(?:\b(?:rue|avenue|av\.|chaussée|ch(?:aus)?sée|chaussee|boulevard|blvd|bd|place|pl|allée|quai|"
    r"chemin|route|impasse|passage)\b[\s\w\-\.]+?\d+.*?\b\d{4}\b.*?[A-Za-zÀ-Ÿ\-]+)",
    re.IGNORECASE,
)
_CP_VILLE_RE = re.compile(r"\b(\d{4})\s+([A-Za-zÀ-Ÿ\-]{3,})\b")
_EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.[a-z]{2,}")
_TEL_BE_RE = re.compile(r"(?:\+32|0032|0)(?:\s*\d){8,9}")
_DANIEL_SIG = ("detectivebelgique", "daniel hurchon", "0779.433.503", "chaussée bara")


def _is_daniel_sig(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _DANIEL_SIG)


def _extract_entreprise_from_text(text: str) -> dict | None:
    """Extraction regex d'infos entreprise depuis texte brut."""
    if not text or len(text) < 30:
        return None
    m = _ENTREPRISE_NAME_RE.search(text)
    if not m:
        return None
    nom = m.group(1).strip()
    nom = re.sub(r"\s+", " ", nom)
    if _is_daniel_sig(nom):
        return None

    tva = None
    tva_m = _TVA_BE_RE.search(text)
    if tva_m:
        tva = tva_m.group(0).upper().replace(" ", "").replace(".", "")

    adresse = None
    for addr_m in _ADRESSE_BE_RE.finditer(text):
        candidate = addr_m.group(0).strip()
        if not _is_daniel_sig(candidate):
            adresse = re.sub(r"\s+", " ", candidate)
            break
    if not adresse:
        cp_m = _CP_VILLE_RE.search(text)
        if cp_m:
            cp, ville = cp_m.groups()
            if not _is_daniel_sig(f"{cp} {ville}"):
                adresse = f"{cp} {ville}"

    emails = []
    for em in _EMAIL_RE.finditer(text):
        e = em.group(0)
        if _is_daniel_sig(e):
            continue
        e_lower = e.lower()
        domain = e_lower.split("@")[-1].replace(".", "").replace("-", "")
        nom_norm = nom.lower().replace(" ", "").replace("-", "")
        if nom_norm in domain:
            emails.insert(0, e)
        else:
            emails.append(e)
    emails = list(dict.fromkeys(emails))

    tels = []
    for tm in _TEL_BE_RE.finditer(text):
        raw = tm.group(0)
        digits = re.sub(r"\D", "", raw)
        if digits.endswith("433503"):
            continue
        tels.append(raw.strip())
    tels = list(dict.fromkeys(tels))

    if not any((tva, adresse, emails, tels)):
        return None
    return {"nom": nom, "tva": tva, "adresse": adresse, "emails": emails, "telephones": tels}


async def _extract_and_feed_entreprise(
    text: str,
    subject: str,
    dossier_id: str | None,
    marque: str,
    date_str: str,
    base_url: str,
    api_secret: str,
    source_label: str = "email",
) -> None:
    """Extrait les infos entreprise et crée/merge une fiche dans Cerveau2."""
    info = _extract_entreprise_from_text(text)
    if not info:
        return

    nom = info["nom"]
    slug = re.sub(r"[^a-z0-9]+", "-", nom.lower()).strip("-")[:60]
    doc_id = f"entreprise-{slug}"

    lines = [
        f"# {nom}",
        "",
        f"**Dossier** : {dossier_id or 'non assigné'}",
        f"**Source** : {source_label} du {date_str} — {subject}",
        "",
        "## Coordonnées",
    ]
    if info.get("tva"):
        lines.append(f"- **TVA** : {info['tva']}")
    if info.get("adresse"):
        lines.append(f"- **Adresse** : {info['adresse']}")
    if info.get("emails"):
        lines.append(f"- **Emails** : {', '.join(info['emails'])}")
    if info.get("telephones"):
        lines.append(f"- **Téléphones** : {', '.join(info['telephones'])}")

    body = "\n".join(lines)
    metadata = {
        "source": f"extraction_{source_label}",
        "nom": nom,
        "tva": info.get("tva"),
        "adresse": info.get("adresse"),
        "emails": info.get("emails"),
        "telephones": info.get("telephones"),
    }

    asyncio.create_task(
        feed_document(
            doc_id=doc_id,
            type="fiche_entreprise",
            dossier_id=dossier_id,
            marque=marque,
            date=date_str,
            titre=f"Entreprise — {nom}",
            body=body,
            metadata=metadata,
            zone="jaune",
            langue="fr",
            base_url=base_url,
            api_secret=api_secret,
        )
    )
    log.info(
        "poller.entreprise_extracted",
        nom=nom,
        dossier_id=dossier_id,
        doc_id=doc_id,
        source=source_label,
    )


async def _extract_and_feed_contact(
    text: str,
    subject: str,
    sender: str,
    dossier_id: str | None,
    marque: str,
    date_str: str,
    base_url: str,
    api_secret: str,
    source_label: str = "email",
) -> None:
    """Extrait les coordonnées du demandeur et crée une fiche contact dans Cerveau2."""
    if not _CONTACT_SIGNALS_RE.search(text or ""):
        return

    from app.llm.router import complete
    from app.settings_store import get_llm_model_classifier

    prompt = (
        "Extrait les coordonnées de la PERSONNE MENTIONNÉE dans ce message "
        "(client/demandeur, pas l'expéditeur technique ni la signature de Daniel/Detective.be).\n"
        "Réponds UNIQUEMENT en JSON, sans texte autour.\n"
        'Format : {"nom":"...","prenom":"...","adresse":"...","code_postal":"...","ville":"...","telephone":"...","email":"..."}\n'
        "Mets null pour les champs absents. Si aucune coordonnée trouvée, réponds: {}\n\n"
        f"Message :\n{text[:2500]}"
    )
    try:
        raw = await complete(
            model=get_llm_model_classifier(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.0,
        )
    except Exception as e:
        log.warning("poller.contact_llm_failed", error=str(e), source=source_label)
        return

    import json

    data: dict = {}
    try:
        json_match = re.search(r"\{[^{}]*\}", raw or "", re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
    except (json.JSONDecodeError, AttributeError):
        pass

    if not any(v for v in data.values() if v):
        return

    nom = data.get("nom") or ""
    prenom = data.get("prenom") or ""
    nom_complet = " ".join(filter(None, [prenom, nom])) or sender
    doc_id = _normalize_contact_key(
        data.get("email"), data.get("telephone"), nom_complet, dossier_id
    )

    lines = [
        f"# Fiche contact — {nom_complet}",
        "",
        f"**Dossier** : {dossier_id or 'non assigné'}",
        f"**Source** : {source_label} du {date_str} — {subject}",
        "",
        "## Coordonnées",
    ]
    if nom or prenom:
        lines.append(f"- **Nom** : {prenom} {nom}".strip(" -"))
    if data.get("adresse"):
        lines.append(f"- **Adresse** : {data['adresse']}")
    if data.get("code_postal") or data.get("ville"):
        lines.append(
            f"- **Localité** : {data.get('code_postal', '')} {data.get('ville', '')}".strip()
        )
    if data.get("telephone"):
        lines.append(f"- **Téléphone** : {data['telephone']}")
    if data.get("email"):
        lines.append(f"- **Email** : {data['email']}")

    fiche_body = "\n".join(lines)

    asyncio.create_task(
        feed_document(
            doc_id=doc_id,
            type="fiche_contact",
            dossier_id=dossier_id,
            marque=marque,
            date=date_str,
            titre=f"Contact — {nom_complet}",
            body=fiche_body,
            metadata={
                "source": f"extraction_{source_label}",
                "nom": nom,
                "prenom": prenom,
                "telephone": data.get("telephone"),
                "email_contact": data.get("email"),
                "code_postal": data.get("code_postal"),
                "ville": data.get("ville"),
            },
            zone="jaune",
            langue="fr",
            base_url=base_url,
            api_secret=api_secret,
        )
    )
    log.info(
        "poller.contact_extracted",
        nom=nom_complet,
        dossier_id=dossier_id,
        doc_id=doc_id,
        source=source_label,
    )


def _persist(
    db_path: Path,
    imap_uid: str,
    mailbox_name: str,
    subject: str,
    sender: str,
    received_at: str,
    category: str,
    draft_generated: int,
    body_preview: str = "",
    body: str = "",
    ai_draft: str = "",
    priority: str = "normal",
    status: str = "pending",
) -> int:
    """Persiste le mail. En cas de conflit, ne JAMAIS écraser category/priority/status cockpit.

    v1.21.3 : coercion str() défensive sur subject/sender/received_at
    (sqlite3 ne sait pas binder email.header.Header → crash prod observé sur uid 5914).
    """
    subject = str(subject) if subject is not None else ""
    sender = str(sender) if sender is not None else ""
    received_at = str(received_at) if received_at is not None else ""
    body_preview = str(body_preview) if body_preview is not None else ""
    body = str(body) if body is not None else ""
    ai_draft = str(ai_draft) if ai_draft is not None else ""
    conn = sqlite3.connect(db_path)
    try:
        # Vérifier existence
        row = conn.execute(
            "SELECT id FROM mail_processed WHERE imap_uid = ? AND mailbox_name = ?",
            (imap_uid, mailbox_name),
        ).fetchone()
        if row:
            mail_id = row[0]
            # Mise à jour minimale : enrichissement seulement, champs cockpit protégés
            conn.execute(
                """
                UPDATE mail_processed SET
                    draft_generated = COALESCE(NULLIF(?, 0), draft_generated),
                    body_preview = COALESCE(NULLIF(?, ''), body_preview),
                    body = COALESCE(NULLIF(?, ''), body),
                    ai_draft = COALESCE(NULLIF(?, ''), ai_draft),
                    processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (draft_generated, body_preview, body, ai_draft, mail_id),
            )
            conn.commit()
            return mail_id

        # Nouveau mail
        cursor = conn.execute(
            """
            INSERT INTO mail_processed
                (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
                 body_preview, body, ai_draft, status, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                imap_uid,
                mailbox_name,
                subject,
                sender,
                received_at,
                category,
                draft_generated,
                body_preview,
                body,
                ai_draft,
                status,
                priority,
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


async def poll_mailbox(mailbox: MailboxConfig, stop_event: asyncio.Event) -> None:
    """Boucle de polling IMAP pour une boîte."""
    settings = get_settings()
    interval = settings.poll_interval_seconds
    log.info("poller.start", mailbox=mailbox.name, interval=interval)

    while not stop_event.is_set():
        try:
            await _poll_once(mailbox)
            health.mark_cycle(mailbox.name)
        except Exception as e:
            log.exception("poller.error", mailbox=mailbox.name, error=str(e))
            health.mark_imap(mailbox.name, False)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


async def _poll_once(mailbox: MailboxConfig) -> None:
    for attempt in range(1, IMAP_RETRY_ATTEMPTS + 1):
        try:
            await _process_mailbox(mailbox)
            return
        except Exception as e:
            if attempt == IMAP_RETRY_ATTEMPTS:
                log.error("poller.gave_up", mailbox=mailbox.name, error=str(e))
                return
            backoff = 2**attempt
            log.warning(
                "poller.retry",
                mailbox=mailbox.name,
                attempt=attempt,
                backoff=backoff,
                error=str(e),
            )
            await asyncio.sleep(backoff)


def _extract_attachments(msg: Message) -> list[tuple[str, bytes]]:
    """Extrait les pièces jointes supportées d'un email multipart.

    Retourne une liste de (filename, data_bytes) pour les formats
    que document_extract sait parser. Ignore les exécutables, les
    images signature/logo vides, et les formats non supportés.
    """
    if not msg.is_multipart():
        return []

    results: list[tuple[str, bytes]] = []
    seen_names: set[str] = set()

    for part in msg.walk():
        filename = part.get_filename() or ""
        if not filename or filename in seen_names:
            continue
        seen_names.add(filename)

        # Ignorer les exécutables
        if filename.lower().endswith((".exe", ".zip", ".js", ".vbs", ".scr", ".bat", ".cmd")):
            continue

        if not is_supported(filename):
            continue

        payload = part.get_payload(decode=True)
        if not payload or len(payload) == 0:
            continue

        # Heuristique : ignorer les mini-images (probablement logo/signature)
        if filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            if len(payload) < 2048:
                continue

        results.append((filename, payload))

    return results


def _save_attachments(
    db_path: Path,
    mail_id: int,
    attachments: list[tuple[str, bytes]],
    data_dir: Path,
) -> None:
    """Write attachments to disk and track them in email_attachment table."""
    att_dir = data_dir / "attachments" / str(mail_id)
    att_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        for filename, data in attachments:
            safe_name = filename.replace("/", "_").replace("\\", "_")
            storage_path = att_dir / safe_name
            storage_path.write_bytes(data)

            text_preview = ""
            try:
                txt = extract_text_bytes(data, filename)
                if txt:
                    text_preview = txt[:3000]
            except Exception:
                pass

            # Stocker un chemin relatif pour être portable Mac → VPS
            rel_path = storage_path.relative_to(data_dir)
            conn.execute(
                """
                INSERT INTO email_attachment
                    (mail_processed_id, filename, storage_path, size_bytes, extracted_text_preview)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mail_id, filename, str(rel_path), len(data), text_preview),
            )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_attachments(db_path: Path, data_dir: Path, retention_days: int = 30) -> None:
    """Purge attachments older than retention_days from disk and DB."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, storage_path FROM email_attachment WHERE created_at < datetime('now', '-' || ? || ' days')",
            (retention_days,),
        )
        rows = cursor.fetchall()
        for _id, storage_path in rows:
            try:
                Path(storage_path).unlink(missing_ok=True)
                # Also try to remove empty parent dir
                parent = Path(storage_path).parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception:
                pass
            conn.execute("DELETE FROM email_attachment WHERE id = ?", (_id,))
        conn.commit()
        if rows:
            log.info("attachments.purged", count=len(rows), retention_days=retention_days)
    finally:
        conn.close()


def _is_verified_demande_client(category: str, msg: Message) -> bool:
    """Garde-fou final : même si le LLM dit 'demande_client', on bloque les
    emails automatiques évidents avant de notifier Slack."""
    if category != "demande_client":
        return False

    sender = str(msg.get("From", "") or "").lower()
    subject = str(msg.get("Subject", "") or "").lower()
    body = _get_body_text(msg).lower()

    # Expéditeur de service connu
    if any(s in sender for s in _SERVICE_SENDERS):
        return False
    # Headers d'email automatique
    if msg.get("Auto-Submitted") or msg.get("X-Auto-Response-Suppress"):
        return False
    # Sujets typiques d'emails automatiques
    auto_keywords = (
        "renouvellement",
        "renewal",
        "confirmation",
        "reçu",
        "receipt",
        "facture",
        "invoice",
        "votre abonnement",
        "your subscription",
        "payment received",
        "paiement reçu",
        "alerte",
        "notification",
    )
    if any(kw in subject for kw in auto_keywords):
        return False

    # Body typique d'un email corporate / newsletter / auto (non demande client).
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
        "this email was sent by",
        "you are receiving this",
        "unsubscribe",
        "view in browser",
        "manage your preferences",
    )
    if any(m in body for m in auto_body_markers):
        return False

    # Réponses automatiques / Out-of-office.
    if any(
        m in body for m in ("out of office", "absent du bureau", "automatic reply", "auto-reply")
    ):
        return False

    return True


def _build_search_criteria(settings) -> str:
    """Construit le critère SEARCH IMAP : UNKEYWORD AgentProcessed.

    Le critère SINCE a été retiré (v1.17.2) car le serveur IMAP Infomaniak
    rejette silencieusement le format RFC 3501, causant un retour de 0 résultats.
    L'idempotence est assurée par le flag AgentProcessed + le check _mail_exists.
    """
    if settings.process_since_date:
        log.warning(
            "imap.search.since_ignored",
            reason="Infomaniak silently rejects SINCE date format in SEARCH. "
            "Relying on UNKEYWORD AgentProcessed + DB idempotence instead.",
        )
    return f"UNKEYWORD {AGENT_FLAG}"


async def _process_mailbox(mailbox: MailboxConfig) -> None:
    settings = get_settings()
    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    await client.wait_hello_from_server()
    login_resp = await client.login(mailbox.user, mailbox.app_password)
    if login_resp.result != "OK":
        log.warning("imap.login_failed", mailbox=mailbox.name, response=login_resp)
        await client.logout()
        return

    try:
        select_resp = await client.select("INBOX")
        if select_resp.result != "OK":
            raise RuntimeError(f"SELECT INBOX failed: {select_resp}")

        search_criteria = _build_search_criteria(settings)
        search_resp = await client.search(search_criteria)
        if search_resp.result != "OK":
            raise RuntimeError(f"SEARCH failed: {search_resp}")

        uids = search_resp.lines[0].split() if search_resp.lines else []
        log.info("poller.found", mailbox=mailbox.name, count=len(uids))

        # Limiter le traitement par cycle pour ne pas bloquer l'event loop
        # quand le backlog est important (ex: suppression du critère SINCE).
        MAX_PER_CYCLE = 10
        if len(uids) > MAX_PER_CYCLE:
            log.warning(
                "poller.backlog_limited",
                mailbox=mailbox.name,
                total=len(uids),
                cycle_max=MAX_PER_CYCLE,
            )
            uids = uids[::-1][:MAX_PER_CYCLE]

        cycle_stats: dict[str, int] = {}
        for uid_bytes in uids:
            uid = uid_bytes.decode()
            try:
                cat = await _process_single_mail(client, uid, mailbox)
                cycle_stats[cat] = cycle_stats.get(cat, 0) + 1
            except Exception:
                log.exception("poller.mail_error", mailbox=mailbox.name, uid=uid)
            # Céder le contrôle à l'event loop pour que uvicorn/web restent réactifs
            await asyncio.sleep(0.5)

        if cycle_stats:
            log.info(
                "poller.cycle_summary",
                mailbox=mailbox.name,
                processed=sum(cycle_stats.values()),
                breakdown=cycle_stats,
            )
            details = f"processed={sum(cycle_stats.values())} breakdown={cycle_stats}"
            cycle_result = "ok"
            # v1.21.3 : reset compteur d'erreurs consécutives si ≥ 1 mail OK
            health.reset_errors(mailbox.name)
        else:
            log.info("poller.cycle_empty", mailbox=mailbox.name)
            details = "processed=0"
            cycle_result = "empty"

        await asyncio.to_thread(
            _log_telemetry,
            settings.db_agent_state,
            "poller_cycle",
            mailbox.name,
            details,
        )
        # v1.21.7 : audit log systématique à chaque fin de cycle (vides inclus)
        # pour traçabilité client — Daniel peut vérifier dans /audit que le
        # service tourne même quand 0 mail n'est trouvé.
        await asyncio.to_thread(
            _log_audit,
            settings.db_agent_state,
            mailbox.name,
            cycle_result,
            details,
        )

        await client.logout()
        health.mark_imap(mailbox.name, True)
    except Exception:
        with contextlib.suppress(Exception):
            await client.close()
        raise


async def _maybe_alert_poller_failure(
    mailbox: MailboxConfig,
    consecutive_errors: int,
    last_error: str,
    sample_uids: list[str],
) -> None:
    """Déclenche l'alerte poller (Resend) quand le seuil est franchi. Fire-and-forget.

    v1.21.3 : asyncio.create_task() ne fige pas la hot path du poller.
    """
    settings = get_settings()
    if consecutive_errors < settings.poller_alert_threshold:
        return
    from app.alerts import alert_poller_persistent_failure

    asyncio.create_task(
        alert_poller_persistent_failure(
            mailbox_name=mailbox.name,
            error_count=consecutive_errors,
            last_error=last_error,
            sample_uids=sample_uids[:5],
        )
    )


async def _process_single_mail(
    client: aioimaplib.IMAP4,
    uid: str,
    mailbox: MailboxConfig,
) -> str:
    settings = get_settings()
    language = mailbox.default_lang
    try:
        fetch_resp = await client.fetch(uid, "RFC822")
        if fetch_resp.result != "OK":
            raise RuntimeError(f"FETCH {uid} failed: {fetch_resp}")

        if len(fetch_resp.lines) < 2:
            raise RuntimeError(f"FETCH {uid} returned empty body")

        rfc822_bytes = fetch_resp.lines[1]
        msg = message_from_bytes(rfc822_bytes)

        sender_raw = msg.get("From", "")
        subject_raw = msg.get("Subject", "")
        sender = _decode_header(parseaddr(sender_raw)[1] or sender_raw)
        subject = _decode_header(subject_raw)
        received_at = str(msg.get("Date", "") or "")

        # Filtre date critique : ne traiter que les mails depuis le 1er juin 2026.
        # Les mails plus vieux sont flaggés comme traités pour nettoyer le backlog.
        if received_at:
            try:
                dt = parsedate_to_datetime(received_at)
                if dt.date() < datetime(2026, 6, 1).date():
                    if not settings.dry_run:
                        store_resp = await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")
                        if store_resp.result != "OK":
                            log.warning(
                                "poller.flag_failed",
                                mailbox=mailbox.name,
                                uid=uid,
                                response=store_resp,
                            )
                    log.info(
                        "poller.date_skipped",
                        mailbox=mailbox.name,
                        uid=uid,
                        date=received_at[:10],
                        reason="before_2026-06-01",
                    )
                    return "skipped"
            except Exception:
                pass  # Si parsing échoue, on traite quand même

        message_id = msg.get("Message-ID", "")
        body = _get_body_text(msg)

        # Skip système : emails auto-générés (magic links Resend, etc.)
        if _is_system_email(sender):
            log.info(
                "poller.system_email_skipped",
                mailbox=mailbox.name,
                uid=uid,
                sender=sender,
                subject=subject,
            )
            if not settings.dry_run:
                store_resp = await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")
                if store_resp.result != "OK":
                    log.warning(
                        "poller.flag_failed", mailbox=mailbox.name, uid=uid, response=store_resp
                    )
            return "skipped"

        log.info(
            "poller.new_mail",
            mailbox=mailbox.name,
            uid=uid,
            sender=sender,
            subject=subject,
            message_id=message_id,
        )

        # v1.25.3 — correction du sujet si homoglyphes (ex: itsme cyrillique #614).
        # Coût LLM nul (forfait Ollama Pro). Dégradation silencieuse si LLM échoue :
        # on conserve le sujet original. Le sujet corrigé bénéficie à classify,
        # assign_priority, generate_draft (sujet lisible du brouillon V2a) et la persistance.
        if is_subject_suspect(subject):
            fixed = await fix_subject_llm(subject, body)
            if fixed:
                log.info(
                    "poller.subject_fixed",
                    mailbox=mailbox.name,
                    uid=uid,
                    subject_original=subject[:120],
                    subject_fixed=fixed[:120],
                )
                subject = fixed

        # v1.25.4 — tag [NO_EMAIL_IN_THE_FORM] si sender = forwarder WordPress
        # (formulaires WP sans email client, vrai contact = téléphone, cf. Task #4).
        tagged = tag_no_email(subject, sender)
        if tagged != subject:
            log.info(
                "poller.subject_tagged_no_email",
                mailbox=mailbox.name,
                uid=uid,
                sender=sender,
            )
            subject = tagged

        prefilter_category = quick_classify(msg)
        if prefilter_category:
            # Garde anti-faux-positif phishing : si l'expéditeur est déjà connu
            # (présent dans mail_processed), ne pas forcer phishing via prefilter.
            # On laisse le LLM classifier décider à la place.
            if prefilter_category == "phishing" and await asyncio.to_thread(
                _is_known_sender, settings.db_agent_state, sender
            ):
                category = await classify(subject, body, sender)
                log.info(
                    "poller.prefilter_phishing_guard",
                    mailbox=mailbox.name,
                    uid=uid,
                    sender=sender,
                    llm_category=category,
                )
            else:
                category = prefilter_category
                log.info("poller.prefilter", mailbox=mailbox.name, uid=uid, category=category)
        else:
            category = await classify(subject, body, sender)
            log.info("poller.classified", mailbox=mailbox.name, uid=uid, category=category)

        priority = assign_priority(category, subject, body, sender)
        # Garde-fous inconditionnels
        if category == "demande_client":
            priority = "high"  # business vital
        elif category == "phishing":
            priority = "high"  # menace sécurité
        elif category == "autre":
            priority = "low"  # rien à traiter
        # Newsletter / calendrier : auto-approved + low priority (rien à traiter)
        status = "pending"
        text_lower = f"{subject} {body}".lower()
        if (
            category == "newsletter"
            or category == "spam"
            or (
                category == "autre"
                and any(
                    kw in text_lower
                    for kw in (
                        "invitation",
                        "calendar",
                        "ical",
                        "vcalendar",
                        "event",
                        "meeting request",
                        "updated invitation",
                        "invitation updated",
                        "accepté",
                        "refusé",
                        "tentative",
                        "provisoire",
                    )
                )
            )
        ):
            status = "approved"
            priority = "low"
        log.info(
            "poller.priority", mailbox=mailbox.name, uid=uid, category=category, priority=priority
        )

        is_new = not await asyncio.to_thread(
            _mail_exists, settings.db_agent_state, uid, mailbox.name
        )

        body_preview = body[:2000] if body else ""
        draft_generated = 0
        verified_draft = False
        gen = None
        if category == "demande_client" and is_new:
            language = detect_language(body, default=mailbox.default_lang)
            is_followup = await asyncio.to_thread(
                _is_client_followup, settings.db_agent_state, sender, msg
            )
            if is_followup:
                log.info(
                    "poller.followup_response_detected",
                    mailbox=mailbox.name,
                    uid=uid,
                    sender=sender,
                    subject=subject,
                )
            gen = await generate_draft(
                subject,
                body,
                sender,
                mailbox,
                language,
                category,
                is_followup_response=is_followup,
            )
            draft_generated = 1
            verified_draft = _is_verified_demande_client(category, msg)

        ai_draft_text = ""
        if category == "demande_client" and draft_generated:
            ai_draft_text = gen.draft

        mail_id = await asyncio.to_thread(
            _persist,
            settings.db_agent_state,
            uid,
            mailbox.name,
            subject,
            sender,
            received_at,
            category,
            draft_generated,
            body_preview,
            body,
            ai_draft_text,
            priority,
            status,
        )

        # --- Sauvegarde locale des pièces jointes (tous les emails) ---
        attachments = _extract_attachments(msg)
        if attachments:
            await asyncio.to_thread(
                _save_attachments,
                settings.db_agent_state,
                mail_id,
                attachments,
                settings.data_dir,
            )
            log.info(
                "poller.attachments_saved",
                mailbox=mailbox.name,
                uid=uid,
                count=len(attachments),
                mail_id=mail_id,
            )

        # --- Alimentation Cerveau2 (tout sauf newsletter / phishing) ---
        if category not in ("newsletter", "phishing") and not settings.dry_run:
            dossier_id = derive_dossier_id(
                sender=sender,
                subject=subject,
                marque=mailbox.name,
            )
            date_str = ""
            heure_str = ""
            try:
                dt = parsedate_to_datetime(received_at)
                date_str = dt.strftime("%Y-%m-%d")
                heure_str = dt.strftime("%H:%M")
            except Exception:
                date_str = received_at[:10] if received_at else ""
                heure_str = ""

            _marque = _MARQUE_CERVEAU2.get(mailbox.name, mailbox.name)

            _task = asyncio.create_task(  # noqa: RUF006
                feed_correspondance(
                    message_id=message_id or f"{mailbox.name}_{uid}",
                    direction="in",
                    date=date_str,
                    heure=heure_str,
                    expediteur=sender,
                    destinataire=mailbox.user,
                    objet=subject,
                    body=body,
                    marque=_marque,
                    dossier_id=dossier_id,
                    categorie=category,
                    zone="jaune",
                    langue=language,
                    priorite=priority,
                    base_url=settings.cerveau2_base_url,
                    api_secret=settings.cerveau2_api_secret,
                )
            )

            # --- Extraction fiche entreprise depuis le body (regex, pas de LLM) ---
            asyncio.create_task(  # noqa: RUF006
                _extract_and_feed_entreprise(
                    text=body,
                    subject=subject,
                    dossier_id=dossier_id,
                    marque=_marque,
                    date_str=date_str,
                    base_url=settings.cerveau2_base_url,
                    api_secret=settings.cerveau2_api_secret,
                    source_label="email",
                )
            )

            # --- Extraction fiche contact depuis le body (tous sauf newsletter/phishing) ---
            asyncio.create_task(  # noqa: RUF006
                _extract_and_feed_contact(
                    text=body,
                    subject=subject,
                    sender=sender,
                    dossier_id=dossier_id,
                    marque=_marque,
                    date_str=date_str,
                    base_url=settings.cerveau2_base_url,
                    api_secret=settings.cerveau2_api_secret,
                    source_label="email",
                )
            )

            # --- Pièces jointes -> Cerveau2 (skip newsletter / phishing : bruit) ---
            if category not in ("newsletter", "phishing"):
                for att_filename, att_data in attachments:
                    att_text = extract_text_bytes(att_data, att_filename)
                    att_extractable = bool(att_text and att_text.strip())
                    if not att_extractable:
                        # Fallback : même non extractable, on ingère avec métadonnées pour référence
                        att_text = (
                            f"[Pièce jointe non extractable automatiquement]\n"
                            f"Fichier : {att_filename}\n"
                            f"Taille : {len(att_data)} octets\n"
                            f"Type : {Path(att_filename).suffix.lower() or 'inconnu'}"
                        )
                        log.info(
                            "poller.attachment_unextractable",
                            mailbox=mailbox.name,
                            uid=uid,
                            filename=att_filename,
                            dossier_id=dossier_id,
                            size=len(att_data),
                        )

                    # Hash déterministe (MD5) pour doc_id stable entre les redémarrages
                    att_hash = hashlib.md5(
                        f"{mail_id}:{att_filename}".encode(), usedforsecurity=False
                    ).hexdigest()[:12]
                    att_id = f"att-{mail_id}-{att_hash}"

                    asyncio.create_task(  # noqa: RUF006
                        feed_document(
                            doc_id=att_id,
                            type="document",
                            dossier_id=dossier_id,
                            marque=_marque,
                            date=date_str,
                            titre=f"[PJ] {att_filename}",
                            body=att_text,
                            metadata={
                                "source": "piece_jointe_email",
                                "parent_message_id": message_id or f"{mailbox.name}_{uid}",
                                "filename": att_filename,
                                "size_bytes": len(att_data),
                            },
                            zone="jaune",
                            langue=language,
                            base_url=settings.cerveau2_base_url,
                            api_secret=settings.cerveau2_api_secret,
                        )
                    )
                    log.info(
                        "poller.attachment_ingested",
                        mailbox=mailbox.name,
                        uid=uid,
                        filename=att_filename,
                        dossier_id=dossier_id,
                        size=len(att_data),
                        doc_id=att_id,
                    )

                    # --- Extraction fiche entreprise depuis la pièce jointe ---
                    if att_extractable:
                        asyncio.create_task(  # noqa: RUF006
                            _extract_and_feed_entreprise(
                                text=att_text,
                                subject=att_filename,
                                dossier_id=dossier_id,
                                marque=_marque,
                                date_str=date_str,
                                base_url=settings.cerveau2_base_url,
                                api_secret=settings.cerveau2_api_secret,
                                source_label=f"pièce jointe [{att_filename}]",
                            )
                        )

                    # --- Extraction fiche contact depuis la pièce jointe (si texte extractable) ---
                    if att_extractable:
                        asyncio.create_task(  # noqa: RUF006
                            _extract_and_feed_contact(
                                text=att_text,
                                subject=att_filename,
                                sender=sender,
                                dossier_id=dossier_id,
                                marque=_marque,
                                date_str=date_str,
                                base_url=settings.cerveau2_base_url,
                                api_secret=settings.cerveau2_api_secret,
                                source_label=f"pièce jointe [{att_filename}]",
                            )
                        )

        # v1.22.7 : générer un brouillon pour toute catégorie configurée (demande_client + prise_contact)
        draft_categories = {
            c.strip().lower() for c in settings.draft_categories.split(",") if c.strip()
        }
        if category.lower() in draft_categories and is_new and not settings.dry_run:
            incoming = IncomingMail(
                sender=sender,
                subject=subject,
                body=body,
                received_at=received_at,
                message_id=message_id,
            )
            draft_ok = await append_draft(
                incoming, mailbox, gen, mail_id=mail_id, imap_client=client
            )
            _log_telemetry(
                db_path=settings.db_agent_state,
                event_type="draft_deposited" if draft_ok else "draft_failed",
                mailbox_name=mailbox.name,
                details=f"mail_id={mail_id} sender={sender} subject={subject[:60]}",
            )
            if not draft_ok:
                await notify_draft(incoming, mailbox, gen, mail_id=mail_id)
                await alert_imap_draft_failure(
                    mailbox_name=mailbox.name,
                    mail_id=mail_id,
                    sender=sender,
                    subject=subject,
                    error_hint="Connexion IMAP secondaire rejetée (probable) ou LIST/APPEND échoué",
                )
            if verified_draft:
                await notify_slack_draft(
                    draft_id=mail_id,
                    sender=sender,
                    subject=subject,
                    category=category,
                    body_preview=body_preview,
                    base_url=settings.public_base_url.rstrip("/")
                    if settings.public_base_url
                    else "",
                )
            else:
                log.info(
                    "slack.notify_skipped",
                    mailbox=mailbox.name,
                    uid=uid,
                    reason="unverified_automatic_email",
                    sender=sender,
                    subject=subject,
                )
        elif category.lower() in draft_categories and is_new and settings.dry_run:
            log.info(
                "dry_run.skip_draft",
                mailbox=mailbox.name,
                uid=uid,
                recipient=settings.draft_recipient,
                verified=verified_draft,
                category=category,
            )

        if not settings.dry_run:
            store_resp = await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")
            if store_resp.result != "OK":
                log.warning(
                    "poller.flag_failed", mailbox=mailbox.name, uid=uid, response=store_resp
                )
        else:
            log.info("dry_run.skip_flag", mailbox=mailbox.name, uid=uid)

        return category
    except Exception as e:
        # v1.21.3 : try/except englobant. Libère la queue IMAP (flag AgentAttempted)
        # pour ne PAS rejouer ce mail indéfiniment, écrit la télémétrie de crash,
        # incrémente le compteur d'erreurs consécutives, et alerte si seuil franchi.
        log.exception("poller.mail_crash", mailbox=mailbox.name, uid=uid, error=str(e))
        try:
            await asyncio.to_thread(
                _log_telemetry,
                settings.db_agent_state,
                "poller_mail_crash",
                mailbox.name,
                f"uid={uid} error={type(e).__name__}: {e}",
            )
        except Exception:
            pass
        consecutive_errors = health.mark_error(mailbox.name)
        last_err = f"{type(e).__name__}: {e}"
        try:
            await _maybe_alert_poller_failure(mailbox, consecutive_errors, last_err, [uid])
        except Exception:
            pass
        if not settings.dry_run:
            try:
                store_resp = await client.store(uid, "+FLAGS", f"({AGENT_ATTEMPTED_FLAG})")
                if store_resp.result != "OK":
                    log.warning("poller.flag_attempted_failed", mailbox=mailbox.name, uid=uid)
            except Exception as flag_err:
                log.warning(
                    "poller.flag_attempted_store_error",
                    mailbox=mailbox.name,
                    uid=uid,
                    error=str(flag_err),
                )
        return "error"
