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


_MAX_BODY_PREVIEW_SLACK = 400


async def notify_new_draft(
    draft_id: str | int,
    sender: str,
    subject: str,
    category: str,
    body_preview: str = "",
    base_url: str = "",
) -> None:
    """Notification quand un nouveau brouillon est généré."""
    id_display = f"#{draft_id}"
    cockpit_url = ""
    if base_url:
        cockpit_url = f"{base_url}/app/conversation/{draft_id}"
        id_display = f"<{cockpit_url}|#{draft_id}>"

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
    ]

    if body_preview:
        preview = body_preview[:_MAX_BODY_PREVIEW_SLACK]
        if len(body_preview) > _MAX_BODY_PREVIEW_SLACK:
            preview += " …"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Aperçu du mail :*\n```{preview}```",
            },
        })

    if cockpit_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Ouvrir dans le cockpit",
                        "emoji": True,
                    },
                    "url": cockpit_url,
                    "action_id": "open_cockpit",
                }
            ],
        })

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
