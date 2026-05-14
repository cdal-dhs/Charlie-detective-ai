import asyncio
import re
import sqlite3
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

log = structlog.get_logger()

AGENT_FLAG = "$AgentProcessed"
IMAP_RETRY_ATTEMPTS = 3


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
    ai_draft: str = "",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO mail_processed
                (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
                 body_preview, ai_draft, status, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'normal')
            ON CONFLICT(imap_uid, mailbox_name) DO UPDATE SET
                category = excluded.category,
                draft_generated = excluded.draft_generated,
                body_preview = COALESCE(NULLIF(excluded.body_preview, ''), mail_processed.body_preview),
                ai_draft = COALESCE(NULLIF(excluded.ai_draft, ''), mail_processed.ai_draft),
                processed_at = CURRENT_TIMESTAMP
            """,
            (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
             body_preview, ai_draft),
        )
        conn.commit()
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
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


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
            log.warning("poller.retry", mailbox=mailbox.name, attempt=attempt, backoff=backoff, error=str(e))
            await asyncio.sleep(backoff)


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

        search_resp = await client.search("UNSEEN UNKEYWORD $AgentProcessed")
        if search_resp.result != "OK":
            raise RuntimeError(f"SEARCH failed: {search_resp}")

        uids = search_resp.lines[0].split() if search_resp.lines else []
        log.info("poller.found", mailbox=mailbox.name, count=len(uids))

        for uid_bytes in uids:
            uid = uid_bytes.decode()
            try:
                await _process_single_mail(client, uid, mailbox)
            except Exception:
                log.exception("poller.mail_error", mailbox=mailbox.name, uid=uid)

        await client.logout()
        health.mark_imap(mailbox.name, True)
    except Exception:
        try:
            await client.close()
        except Exception:
            pass
        raise


async def _process_single_mail(
    client: aioimaplib.IMAP4,
    uid: str,
    mailbox: MailboxConfig,
) -> None:
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

    draft_generated = 0
    if category == "demande_client":
        language = detect_language(body, default=mailbox.default_lang)
        gen = await generate_draft(subject, body, sender, mailbox, language, category)
        draft_generated = 1

        if not settings.dry_run:
            incoming = IncomingMail(
                sender=sender,
                subject=subject,
                body=body,
                received_at=received_at,
                message_id=message_id,
            )
            await notify_draft(incoming, mailbox, gen)
            await notify_slack_draft(
                draft_id=uid, sender=sender, subject=subject, category=category
            )
        else:
            log.info(
                "dry_run.skip_notify",
                mailbox=mailbox.name,
                uid=uid,
                recipient=settings.draft_recipient,
            )

    body_preview = body[:2000] if body else ""
    ai_draft_text = gen.draft if (category == "demande_client" and draft_generated) else ""

    await asyncio.to_thread(
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
        ai_draft_text,
    )

    if not settings.dry_run:
        store_resp = await client.store(uid, "+FLAGS", f"({AGENT_FLAG})")
        if store_resp.result != "OK":
            log.warning("poller.flag_failed", mailbox=mailbox.name, uid=uid, response=store_resp)
    else:
        log.info("dry_run.skip_flag", mailbox=mailbox.name, uid=uid)
