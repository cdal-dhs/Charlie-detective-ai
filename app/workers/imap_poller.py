import asyncio
import contextlib
import re
import sqlite3
from datetime import datetime
from email import header, message_from_bytes
from email.message import Message
from email.utils import parseaddr
from pathlib import Path

import structlog
from aioimaplib import aioimaplib

from app.config import MailboxConfig, get_settings
from app.delivery.resend_notifier import IncomingMail, notify_draft
from app.delivery.slack_notifier import notify_new_draft as notify_slack_draft
from app.healthcheck import health
from app.pipeline.classifier import classify
from app.pipeline.generator import generate_draft
from app.pipeline.language import detect_language
from app.pipeline.prefilter import quick_classify
from app.pipeline.priority import assign_priority

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
                    html = payload.decode("utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", "", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", "", text)
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
) -> int:
    """Persiste le mail et retourne l'id SQLite auto-incrémenté (pour liens cockpit)."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO mail_processed
                (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
                 body_preview, body, ai_draft, status, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(imap_uid, mailbox_name) DO UPDATE SET
                category = excluded.category,
                draft_generated = excluded.draft_generated,
                body_preview = COALESCE(NULLIF(excluded.body_preview, ''),
                                        mail_processed.body_preview),
                body = COALESCE(NULLIF(excluded.body, ''), mail_processed.body),
                ai_draft = COALESCE(NULLIF(excluded.ai_draft, ''), mail_processed.ai_draft),
                priority = excluded.priority,
                processed_at = CURRENT_TIMESTAMP
            """,
            (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
             body_preview, body, ai_draft, priority),
        )
        conn.commit()
        return cursor.lastrowid
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
        category = prefilter_category
        log.info("poller.prefilter", mailbox=mailbox.name, uid=uid, category=category)
    else:
        category = await classify(subject, body, sender)
        log.info("poller.classified", mailbox=mailbox.name, uid=uid, category=category)

    priority = assign_priority(category, subject, body, sender)
    log.info("poller.priority", mailbox=mailbox.name, uid=uid, category=category, priority=priority)

    body_preview = body[:2000] if body else ""
    draft_generated = 0
    verified_draft = False
    if category == "demande_client":
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
    )

    if category == "demande_client" and not settings.dry_run:
        incoming = IncomingMail(
            sender=sender,
            subject=subject,
            body=body,
            received_at=received_at,
            message_id=message_id,
        )
        await notify_draft(incoming, mailbox, gen)
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
    elif category == "demande_client" and settings.dry_run:
        log.info(
            "dry_run.skip_notify",
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
