import asyncio
import html
import re
import sqlite3
from email import message_from_bytes
from email.message import Message

import aioimaplib

from app.config import get_settings
from app.delivery.imap_draft import _find_drafts_folder
from app.pipeline.language import detect_language


def _html_to_text(raw: str) -> str:
    """Minimal HTML-to-text without external deps."""
    s = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    s = re.sub(r"</p>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def _extract_text(msg: Message) -> str:
    """Best-effort plain text extraction from an RFC822 message."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                text = decoded
                break
            if ctype == "text/html" and not text:
                text = _html_to_text(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            text = _html_to_text(decoded) if msg.get_content_type() == "text/html" else decoded
    return text


async def inspect_mailbox(mb, settings, db_path):
    client = aioimaplib.IMAP4_SSL(host=settings.imap_host, port=settings.imap_port)
    await client.wait_hello_from_server()
    await client.login(mb.user, mb.app_password)
    draft_folder = await _find_drafts_folder(client)
    await client.select(draft_folder)
    _, data = await client.search("ALL")
    uids = data[0].decode().split() if data and data[0] else []
    print(f"\n=== MAILBOX {mb.name} ({mb.brand}) — {len(uids)} drafts ===")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    results = []
    for uid in uids:
        _, msg_data = await client.fetch(uid, "(RFC822)")
        # aioimaplib returns a flat list: [b'1 FETCH (RFC822 {size}',
        # bytearray(content), b')', b'Fetch completed...']
        raw = (
            bytes(msg_data[1])
            if len(msg_data) > 1 and isinstance(msg_data[1], (bytes, bytearray))
            else b""
        )
        if not raw:
            continue

        msg = message_from_bytes(raw)
        subject = msg.get("Subject", "")
        body = _extract_text(msg)

        email_id_match = re.search(r"EMAIL #(\d+)", body)
        email_id = int(email_id_match.group(1)) if email_id_match else None

        # Fallback: older drafts use cockpit link /app/conversation/{id}
        if not email_id:
            conv_match = re.search(r"/app/conversation/(\d+)", body)
            if conv_match:
                email_id = int(conv_match.group(1))

        # Look up original mail in DB
        row = None
        if email_id:
            row = conn.execute(
                "SELECT id, subject, body, category, received_at FROM mail_processed WHERE id=?",
                (email_id,),
            ).fetchone()

        original_body = None
        original_category = None
        received_at = None
        if row:
            original_body, original_category, received_at = (
                row["body"],
                row["category"],
                row["received_at"],
            )

        # Determine language: explicit block > detected from DB body > detected from draft body
        if original_body:
            original_lang = detect_language(original_body, default=mb.default_lang)
        elif "📩 EMAIL D'ORIGINE" in body:
            orig_block = body.split("📩 EMAIL D'ORIGINE")[1]
            orig_block = re.split(r"\n[═─]", orig_block)[0]
            lines = [
                line
                for line in orig_block.split("\n")
                if line.strip() and not line.startswith("Sujet :") and "====" not in line
            ]
            sample = "\n".join(lines[:30])
            original_lang = detect_language(sample, default=mb.default_lang)
        elif body:
            original_lang = detect_language(body, default=mb.default_lang)

        should_be_multilingual = bool(original_lang and original_lang != "fr")

        has_email_id = email_id is not None
        has_original_nl = "📩 EMAIL D'ORIGINE" in body
        has_translation_fr = "🇫🇷 TRADUCTION FR" in body
        has_proposition_fr = "✉️ PROPOSITION DE RÉPONSE (en Français)" in body
        has_translation_prop_nl = "🌍 TRADUCTION DE LA PROPOSITION" in body

        status = []
        if not has_email_id:
            status.append("NO_EMAIL_ID")
        if should_be_multilingual:
            if not has_original_nl:
                status.append("NO_ORIGINAL_NL_BLOCK")
            if not has_translation_fr:
                status.append("NO_FR_TRANSLATION")
            if not has_translation_prop_nl:
                status.append("NO_NL_PROP_TRANSLATION")
        if not has_proposition_fr:
            status.append("NO_FR_PROPOSITION")

        prop_nl_ok = has_translation_prop_nl
        if has_translation_prop_nl:
            prop_nl_section = body.split("🌍 TRADUCTION DE LA PROPOSITION")[1]
            prop_nl_content = re.split(r"\n[═─]", prop_nl_section)[0].strip()
            if len(prop_nl_content) < 50:
                prop_nl_ok = False
                status.append("EMPTY_NL_PROP_TRANSLATION")

        # Extra context for triage
        body_excerpt = re.sub(r"\s+", " ", body[:300]).strip()

        results.append(
            {
                "uid": uid,
                "email_id": email_id,
                "subject": subject[:90],
                "lang": original_lang,
                "category": original_category,
                "received_at": received_at,
                "should_be_multi": should_be_multilingual,
                "status": status,
                "has_email_id": has_email_id,
                "has_original_nl": has_original_nl,
                "has_tr_fr": has_translation_fr,
                "has_prop_fr": has_proposition_fr,
                "has_prop_nl": prop_nl_ok,
                "body_excerpt": body_excerpt,
            }
        )

    conn.close()
    await client.logout()
    return results


async def main():
    settings = get_settings()
    db_path = settings.db_agent_state

    all_results = []
    for mb in settings.mailboxes():
        results = await inspect_mailbox(mb, settings, db_path)
        all_results.extend([(mb.name, r) for r in results])

    print("\n\n=== RÉCAPITULATIF DÉTAILLÉ ===")
    for mb_name, r in all_results:
        if r["status"]:
            print(
                f"\n[{mb_name}] UID {r['uid']} | EMAIL #{r['email_id'] or '?'} | "
                f"lang={r['lang']} | cat={r['category']} | {r['received_at']}"
            )
            print(f"  subject={r['subject']}")
            print(f"  ⚠️  {', '.join(r['status'])}")
            print(f"  excerpt={r['body_excerpt'][:180]}...")

    ok_count = sum(1 for _, r in all_results if not r["status"])
    total = len(all_results)
    print(f"\n✅ OK: {ok_count}/{total}")
    print(f"⚠️  À CORRIGER: {total - ok_count}/{total}")

    nl_to_fix = [(mb, r) for mb, r in all_results if r["should_be_multi"] and r["status"]]
    fr_to_fix = [(mb, r) for mb, r in all_results if not r["should_be_multi"] and r["status"]]
    print(f"\n🟠 NL incomplets: {len(nl_to_fix)}")
    print(f"🔵 FR incomplets (probablement anciens drafts): {len(fr_to_fix)}")


asyncio.run(main())
