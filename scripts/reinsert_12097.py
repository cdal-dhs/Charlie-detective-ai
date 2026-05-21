import asyncio
import html as html_module
import re
import sqlite3
from email import message_from_bytes
from email.utils import parseaddr
from aioimaplib import aioimaplib
from app.config import get_settings
from app.pipeline.language import detect_language
from app.pipeline.generator import generate_draft

async def reinsert():
    settings = get_settings()
    mb = settings.mailboxes()[0]

    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    await client.wait_hello_from_server()
    await client.login(mb.user, mb.app_password)
    await client.select("INBOX")

    fetch_resp = await client.fetch("12097", "RFC822")
    if fetch_resp.result != "OK" or len(fetch_resp.lines) < 2:
        print("FETCH failed")
        return

    rfc822_bytes = bytes(fetch_resp.lines[1])
    msg = message_from_bytes(rfc822_bytes)

    sender_raw = msg.get("From", "")
    subject = msg.get("Subject", "")
    sender = parseaddr(sender_raw)[1] or sender_raw
    received_at = msg.get("Date", "")

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        raw_html = payload.decode("utf-8", errors="replace")
                        body = html_module.unescape(re.sub(r"<[^>]+>", "", raw_html))
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                body = html_module.unescape(re.sub(r"<[^>]+>", "", text))
            else:
                body = text

    body_preview = body[:2000] if body else ""
    print(f"Subject: {subject}")
    print(f"Body preview (first 300): {body_preview[:300]}")

    language = detect_language(body, default=mb.default_lang)
    gen = await generate_draft(subject, body, sender, mb, language, "demande_client")
    ai_draft = gen.draft
    print(f"Draft: {len(ai_draft)} chars")

    conn = sqlite3.connect(str(settings.db_agent_state))
    cursor = conn.execute(
        """
        INSERT INTO mail_processed
            (imap_uid, mailbox_name, subject, sender, received_at, category, draft_generated,
             body_preview, body, ai_draft, status, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        ("12097", mb.name, subject, sender, received_at, "demande_client", 1,
         body_preview, body, ai_draft, "pending", "high"),
    )
    row = cursor.fetchone()
    conn.commit()
    mail_id = row[0] if row else 0
    conn.close()

    await client.uid("STORE", "12097", "+FLAGS", "AgentProcessed")
    await client.logout()

    print(f"SUCCESS: Mail reinserted id={mail_id}")

asyncio.run(reinsert())
