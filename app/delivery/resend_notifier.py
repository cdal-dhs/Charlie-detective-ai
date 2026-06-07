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


def _format_html(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
    mail_id: int | None = None,
    base_url: str = "",
) -> str:
    cockpit_link = ""
    if mail_id and base_url:
        cockpit_link = (
            f'<p style="margin:16px 0;text-align:center;">'
            f'<a href="{base_url}/app/conversation/{mail_id}" '
            f'style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;'
            f'text-decoration:none;border-radius:6px;font-weight:bold;font-size:14px;">'
            f'Ouvrir le dossier #{mail_id} dans le cockpit'
            f'</a></p>'
        )

    return (
        '<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:780px;background:#fff;color:#000;margin:20px;">\n'
        '<div style="margin:0 0 16px;text-align:center;">\n'
        '  <p style="font-size:14px;font-weight:bold;color:#333;margin:0;'
        'letter-spacing:0.3px;">\n'
        '    === PROPOSITION BROUILLON REPONSE PAR Charlie Assistant AI '
        '- Detective.be ===\n'
        '  </p>\n'
        '</div>\n\n'
        '<h2 style="margin:0 0 16px;font-size:18px;">Brouillon généré</h2>\n'
        '<pre style="white-space:pre-wrap;background:#fff;border:1px solid #ccc;'
        'padding:12px;border-radius:6px;font-family:inherit;font-size:14px;color:#000;">\n'
        f'{html.escape(gen.draft)}\n'
        '</pre>\n\n'
        f'{cockpit_link}\n\n'
        '<hr style="border:none;border-top:1px solid #ddd;margin:28px 0;">\n'
        '<h2 style="margin:0 0 16px;font-size:18px;">Message original</h2>\n'
        '<table style="font-size:14px;border-collapse:collapse;'
        'margin-bottom:16px;width:100%;">\n'
        '  <tr><td style="padding:4px 8px 4px 0;vertical-align:top;'
        f'font-weight:bold;width:100px;">Date</td>'
        f'<td style="padding:4px 0;">{html.escape(incoming.received_at or "-")}</td></tr>\n'
        '  <tr><td style="padding:4px 8px 4px 0;vertical-align:top;'
        f'font-weight:bold;width:100px;">Expéditeur</td>'
        f'<td style="padding:4px 0;">{html.escape(incoming.sender)}</td></tr>\n'
        '  <tr><td style="padding:4px 8px 4px 0;vertical-align:top;'
        f'font-weight:bold;">Sujet</td>'
        f'<td style="padding:4px 0;">{html.escape(incoming.subject)}</td></tr>\n'
        '</table>\n'
        '<pre style="white-space:pre-wrap;background:#f6f8fa;border:1px solid #d0d7de;'
        'padding:12px;border-radius:6px;font-family:inherit;font-size:14px;color:#000;">\n'
        f'{html.escape(incoming.body)}\n'
        '</pre>\n\n'
        '<hr style="border:none;border-top:1px solid #ddd;margin:28px 0;">\n'
        '<table style="font-size:12px;color:#555;border-collapse:collapse;'
        'margin-bottom:16px;width:100%;">\n'
        '  <tr><td style="padding:4px 8px 4px 0;vertical-align:top;'
        f'font-weight:bold;width:120px;">Catégorie</td>'
        f'<td style="padding:4px 0;">{html.escape(gen.category)}</td></tr>\n'
        '  <tr><td style="padding:4px 8px 4px 0;vertical-align:top;'
        f'font-weight:bold;">Langue</td>'
        f'<td style="padding:4px 0;">{gen.language}</td></tr>\n'
        '</table>\n'
        '</body></html>'
    )


async def notify_draft(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
    mail_id: int | None = None,
) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("resend.no_key_skip")
        return

    base_url = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
    # v1.21.7 : Daniel en to (le client final), CDAL en cc (traçabilité intégrateur)
    # avant v1.21.7 : tout allait à CDAL → Daniel ne voyait jamais le brouillon
    # en fallback IMAP, et attendait 8 jours pour rien. Bug critique corrigé.
    payload = {
        "from": settings.resend_from,
        "to": [settings.draft_recipient_to],
        "cc": [settings.draft_recipient_cc],
        "subject": f"PROPOSITION REPONSE DETECTIVE - {mail_id}",
        "html": _format_html(incoming, mailbox, gen, mail_id, base_url),
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
    log.info(
        "resend.sent",
        to=settings.draft_recipient_to,
        cc=settings.draft_recipient_cc,
        mailbox=mailbox.name,
    )
