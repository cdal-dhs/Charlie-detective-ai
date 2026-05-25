import asyncio
import contextlib
import hashlib
import html
import re
import sqlite3
from datetime import datetime
from email import header, message_from_bytes
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import structlog
from aioimaplib import aioimaplib

from app.cerveau_client import feed_correspondance, feed_document
from app.cerveau_dossier import derive_dossier_id
from app.config import MailboxConfig, get_settings
from app.delivery.imap_draft import append_draft
from app.delivery.resend_notifier import IncomingMail, notify_draft
from app.delivery.slack_notifier import notify_new_draft as notify_slack_draft
from app.healthcheck import health
from app.pipeline.classifier import classify
from app.pipeline.document_extract import extract_text_bytes, is_supported
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language
from app.pipeline.prefilter import quick_classify
from app.pipeline.priority import assign_priority

# Signaux de coordonnées contact dans un body (tél belge, code postal, adresse)
_CONTACT_SIGNALS_RE = re.compile(
    r'(?:'
    r'0\d[\d\s/\.\-]{6,12}|'               # mobile belge 04xx/...
    r'\+32[\s\d]{8,15}|'                   # +32 ...
    r'\b\d{4}\s+[A-ZÀ-Ÿa-z][a-zà-ÿ]|'    # code postal + ville
    r'(?:Rue|Avenue|Boulevard|Chaussée|Place|Drève|Chemin|Av\.)\s'
    r')',
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
IMAP_RETRY_ATTEMPTS = 3

# Expéditeurs qui ne peuvent JAMAIS être un vrai client
_SERVICE_SENDERS = (
    "infomaniak", "ovh", "stripe", "paypal", "amazon", "microsoft",
    "google", "apple", "meta", "facebook", "linkedin", "twitter", "x.com",
    "github", "gitlab", "sendgrid", "mailgun", "brevo", "mailchimp",
    "hubspot", "zendesk", "intercom", "freshdesk",
)


def _decode_header(value: str) -> str:
    """Décode un header MIME RFC 2047 (ex: =?UTF-8?Q?...?=)."""
    if not value:
        return ""
    decoded_parts = header.decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _get_body_text(msg: Message) -> str:
    """Extraire le texte plain d'un email, ou HTML détaggé en fallback."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    raw_html = payload.decode("utf-8", errors="replace")
                    return html.unescape(re.sub(r"<[^>]+>", "", raw_html))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                return html.unescape(re.sub(r"<[^>]+>", "", text))
            return text
    return ""


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


def _normalize_contact_key(email: str | None, tel: str | None, nom: str, dossier_id: str | None) -> str:
    """Clé de dédup stable : email > tel > nom+dossier."""
    import unicodedata
    def strip_accents(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

    if email:
        return f"contact-{hashlib.md5(email.lower().strip().encode(), usedforsecurity=False).hexdigest()[:12]}"
    if tel:
        tel_clean = re.sub(r"[\s/\.\-]", "", tel)
        return f"contact-{hashlib.md5(tel_clean.encode(), usedforsecurity=False).hexdigest()[:12]}"
    nom_clean = strip_accents(nom.lower().strip()) if nom else "inconnu"
    key = f"{nom_clean}:{dossier_id or 'global'}"
    return f"contact-{hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:12]}"


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
        json_match = re.search(r'\{[^{}]*\}', raw or "", re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
    except (json.JSONDecodeError, AttributeError):
        pass

    if not any(v for v in data.values() if v):
        return

    nom = data.get("nom") or ""
    prenom = data.get("prenom") or ""
    nom_complet = " ".join(filter(None, [prenom, nom])) or sender
    doc_id = _normalize_contact_key(data.get("email"), data.get("telephone"), nom_complet, dossier_id)

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
        lines.append(f"- **Localité** : {data.get('code_postal', '')} {data.get('ville', '')}".strip())
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
    """Persiste le mail. En cas de conflit, ne JAMAIS écraser category/priority/status cockpit."""
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
            (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
             body_preview, body, ai_draft, status, priority),
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
            backoff = 2 ** attempt
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

    sender = (msg.get("From", "") or "").lower()
    subject = (msg.get("Subject", "") or "").lower()

    # Expéditeur de service connu
    if any(s in sender for s in _SERVICE_SENDERS):
        return False
    # Headers d'email automatique
    if msg.get("Auto-Submitted") or msg.get("X-Auto-Response-Suppress"):
        return False
    # Sujets typiques d'emails automatiques
    auto_keywords = (
        "renouvellement", "renewal", "confirmation", "reçu", "receipt",
        "facture", "invoice", "votre abonnement", "your subscription",
        "payment received", "paiement reçu", "alerte", "notification",
    )
    return not any(kw in subject for kw in auto_keywords)


def _build_search_criteria(settings) -> str:
    """Construit le critère SEARCH IMAP : UNKEYWORD AgentProcessed + SINCE si configuré."""
    criteria = ["UNKEYWORD", AGENT_FLAG]
    if settings.process_since_date:
        try:
            dt = datetime.strptime(settings.process_since_date, "%Y-%m-%d")
            since_str = dt.strftime("%d-%b-%Y")
            criteria += ["SINCE", since_str]
        except ValueError:
            log.warning("config.invalid_process_since_date", value=settings.process_since_date)
    return " ".join(criteria)


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

        cycle_stats: dict[str, int] = {}
        for uid_bytes in uids:
            uid = uid_bytes.decode()
            try:
                cat = await _process_single_mail(client, uid, mailbox)
                cycle_stats[cat] = cycle_stats.get(cat, 0) + 1
            except Exception:
                log.exception("poller.mail_error", mailbox=mailbox.name, uid=uid)

        if cycle_stats:
            log.info(
                "poller.cycle_summary",
                mailbox=mailbox.name,
                processed=sum(cycle_stats.values()),
                breakdown=cycle_stats,
            )
            details = f"processed={sum(cycle_stats.values())} breakdown={cycle_stats}"
        else:
            log.info("poller.cycle_empty", mailbox=mailbox.name)
            details = "processed=0"

        await asyncio.to_thread(
            _log_telemetry,
            settings.db_agent_state,
            "poller_cycle",
            mailbox.name,
            details,
        )

        await client.logout()
        health.mark_imap(mailbox.name, True)
    except Exception:
        with contextlib.suppress(Exception):
            await client.close()
        raise


async def _process_single_mail(
    client: aioimaplib.IMAP4,
    uid: str,
    mailbox: MailboxConfig,
) -> str:
    settings = get_settings()
    language = mailbox.default_lang
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
    received_at = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")
    body = _get_body_text(msg)

    log.info(
        "poller.new_mail",
        mailbox=mailbox.name,
        uid=uid,
        sender=sender,
        subject=subject,
        message_id=message_id,
    )

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
        priority = "high"          # business vital
    elif category == "phishing":
        priority = "high"          # menace sécurité
    elif category == "autre":
        priority = "low"           # rien à traiter
    # Newsletter / calendrier : auto-approved + low priority (rien à traiter)
    status = "pending"
    text_lower = f"{subject} {body}".lower()
    if category == "newsletter":
        status = "approved"
        priority = "low"
    elif category == "autre" and any(kw in text_lower for kw in (
        "invitation", "calendar", "ical", "vcalendar", "event",
        "meeting request", "updated invitation", "invitation updated",
        "accepté", "refusé", "tentative", "provisoire",
    )):
        status = "approved"
        priority = "low"
    log.info("poller.priority", mailbox=mailbox.name, uid=uid, category=category, priority=priority)

    is_new = not await asyncio.to_thread(
        _mail_exists, settings.db_agent_state, uid, mailbox.name
    )

    body_preview = body[:2000] if body else ""
    draft_generated = 0
    verified_draft = False
    gen = None
    if category == "demande_client" and is_new:
        language = detect_language(body, default=mailbox.default_lang)
        gen = await generate_draft(subject, body, sender, mailbox, language, category)
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

        # --- Pièces jointes -> Cerveau2 (ZERO tolérance : TOUTES ingérées) ---
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

    if category == "demande_client" and is_new and not settings.dry_run:
        incoming = IncomingMail(
            sender=sender,
            subject=subject,
            body=body,
            received_at=received_at,
            message_id=message_id,
        )
        draft_ok = await append_draft(incoming, mailbox, gen, mail_id=mail_id)
        if not draft_ok:
            # Fallback Resend si APPEND IMAP échoue
            await notify_draft(incoming, mailbox, gen, mail_id=mail_id)
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
    elif category == "demande_client" and is_new and settings.dry_run:
        log.info(
            "dry_run.skip_draft",
            mailbox=mailbox.name,
            uid=uid,
            recipient=settings.draft_recipient,
            verified=verified_draft,
        )

    if not settings.dry_run:
        store_resp = await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")
        if store_resp.result != "OK":
            log.warning("poller.flag_failed", mailbox=mailbox.name, uid=uid, response=store_resp)
    else:
        log.info("dry_run.skip_flag", mailbox=mailbox.name, uid=uid)

    return category
