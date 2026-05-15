"""Module de notification Slack pour Charlie.

Remplace le canal Telegram par un webhook Slack dans le MVP.
Envoi de notifications texte + boutons (via Slack Block Kit).
"""

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()


async def send_slack_message(text: str, blocks: list[dict] | None = None) -> None:
    """Envoie un message texte simple ou structuré (blocks) sur Slack."""
    settings = get_settings()
    if not settings.slack_webhook_url:
        log.warning("slack.no_webhook_skip")
        return

    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(settings.slack_webhook_url, json=payload)
        r.raise_for_status()
    log.info("slack.sent", text=text[:50])


async def notify_new_draft(
    draft_id: str | int,
    sender: str,
    subject: str,
    category: str,
    base_url: str = "",
) -> None:
    """Notification quand un nouveau brouillon est généré."""
    id_display = f"#{draft_id}"
    if base_url:
        id_display = f"<{base_url}/app/conversation/{draft_id}|#{draft_id}>"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Nouveau brouillon généré",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*ID:*\n{id_display}"},
                {"type": "mrkdwn", "text": f"*Expéditeur:*\n{sender}"},
                {"type": "mrkdwn", "text": f"*Sujet:*\n{subject}"},
                {"type": "mrkdwn", "text": f"*Catégorie:*\n{category}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Brouillon à valider dans votre inbox*\nTRIAL DETECTIVE AI : {subject}",
            },
        },
    ]
    await send_slack_message("Nouveau brouillon généré", blocks)


async def notify_digest(subject: str, summary: str) -> None:
    """Notification digest newsletter matinal."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": subject,
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary},
        },
    ]
    await send_slack_message(subject, blocks)
