import html
from dataclasses import dataclass

import httpx
import structlog

from app.config import MailboxConfig, get_settings
from app.pipeline.generator import GenerationResult

log = structlog.get_logger()

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass
class IncomingMail:
    sender: str
    subject: str
    body: str
    received_at: str
    message_id: str


def _format_html(incoming: IncomingMail, mailbox: MailboxConfig, gen: GenerationResult) -> str:
    return f"""\
<html>
  <body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:780px;background:#fff;color:#000;margin:20px;">
    <h2 style="margin:0 0 16px;font-size:18px;">Message original</h2>
    <table style="font-size:14px;border-collapse:collapse;margin-bottom:16px;width:100%;">
      <tr><td style="padding:4px 8px 4px 0;vertical-align:top;font-weight:bold;width:100px;">Expéditeur</td><td style="padding:4px 0;">{html.escape(incoming.sender)}</td></tr>
      <tr><td style="padding:4px 8px 4px 0;vertical-align:top;font-weight:bold;">Sujet</td><td style="padding:4px 0;">{html.escape(incoming.subject)}</td></tr>
    </table>
    <pre style="white-space:pre-wrap;background:#f6f8fa;border:1px solid #d0d7de;padding:12px;border-radius:6px;font-family:inherit;font-size:14px;color:#000;">
{html.escape(incoming.body)}
    </pre>

    <div style="margin:28px 0;text-align:center;">
      <p style="font-size:14px;font-weight:bold;color:#333;margin:0;letter-spacing:0.3px;">
        === PROPOSITION BROUILLON REPONSE PAR Charlie Assiatnt AI - Detective.be ===
      </p>
    </div>

    <h2 style="margin:0 0 16px;font-size:18px;">Brouillon généré</h2>
    <pre style="white-space:pre-wrap;background:#fff;border:1px solid #ccc;padding:12px;border-radius:6px;font-family:inherit;font-size:14px;color:#000;">
{html.escape(gen.draft)}
    </pre>

    <hr style="border:none;border-top:1px solid #ddd;margin:28px 0;">
    <table style="font-size:12px;color:#555;border-collapse:collapse;margin-bottom:16px;width:100%;">
      <tr><td style="padding:4px 8px 4px 0;vertical-align:top;font-weight:bold;width:120px;">Catégorie</td><td style="padding:4px 0;">{html.escape(gen.category)}</td></tr>
      <tr><td style="padding:4px 8px 4px 0;vertical-align:top;font-weight:bold;">Langue</td><td style="padding:4px 0;">{gen.language}</td></tr>
    </table>
  </body>
</html>"""


async def notify_draft(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("resend.no_key_skip")
        return

    payload = {
        "from": settings.resend_from,
        "to": [settings.draft_recipient],
        "subject": f"TRIAL DETECTIVE AI : {incoming.subject}",
        "html": _format_html(incoming, mailbox, gen),
        "headers": {
            "X-Detective-Agent-Mailbox": mailbox.name,
            "X-Detective-Agent-MessageId": incoming.message_id,
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json=payload,
        )
        r.raise_for_status()
    log.info("resend.sent", recipient=settings.draft_recipient, mailbox=mailbox.name)
