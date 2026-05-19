from __future__ import annotations

import contextlib
import re
import time

import structlog

from app.charlie import BOX_ABBR, CharlieResult, ask_charlie
from app.config import get_settings

log = structlog.get_logger()

bolt_app = None
slack_handler = None

_MAX_ROWS = 10
_MAX_FIELDS_PER_ROW = 5
_MAX_SHORT_VAL = 60
_MAX_BODY_VAL = 800
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX = 10
_user_rate_limits: dict[str, list[float]] = {}


def _check_rate_limit(user_id: str) -> bool:
    """Retourne True si l'utilisateur est rate-limité."""
    now = time.time()
    timestamps = _user_rate_limits.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    _user_rate_limits[user_id] = timestamps
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return True
    timestamps.append(now)
    return False


def _strip_bot_mention(text: str) -> str:
    """Supprime le tag <@U...> du début du message."""
    return re.sub(r"^<@U[A-Z0-9]+>\s*", "", text).strip()


def _format_rows_blocks(rows: list[dict], base_url: str) -> list[dict]:
    """Formate les résultats SQL en Block Kit Slack."""
    blocks = []
    for row in rows[:_MAX_ROWS]:
        parts = []
        for key, val in list(row.items()):
            if val is None:
                display = "-"
            elif key in ("body", "ai_draft", "human_draft"):
                display = str(val)[:_MAX_BODY_VAL]
            else:
                display = str(val)[:_MAX_SHORT_VAL]
            if key == "id" and val is not None:
                display = f"<{base_url}/app/conversation/{val}|#{val}>"
            elif key == "subject" and "id" in row and row.get("id") is not None:
                display = f"<{base_url}/app/conversation/{row['id']}|{display}>"
            elif key == "mailbox_name" and val in BOX_ABBR:
                display = BOX_ABBR[val]
            parts.append(f"*{key}:* {display}")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": " | ".join(parts)},
        })
    if len(rows) > _MAX_ROWS:
        msg = f"_({len(rows)} résultats, {_MAX_ROWS} affichés)_"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": msg}],
        })
    return blocks


def format_charlie_response(result: CharlieResult, base_url: str) -> list[dict]:
    """Formate un CharlieResult en Block Kit Slack."""
    blocks = []

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": result.response_text},
    })

    if result.sql and not result.sql_safe:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":warning: _Requête SQL refusée (sécurité)._"},
        })
    elif result.sql and result.sql_error:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":x: _Erreur SQL : {result.sql_error}_"},
        })
    # NOTE : on n'affiche PLUS les rows bruts ici. Le LLM a déjà reçu les
    # données via _sanitize_rows_for_prompt() et a rédigé une réponse
    # conversationnelle. Afficher un dump technique en dessous gâche
    # l'expérience et peut fuiter des données sensibles.
    # Les rows restent disponibles dans l'inbox web, pas dans Slack.

    if result.vault_notes:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f":books: *Second cerveau* — {len(result.vault_notes)} note(s)"}],
        })
        for note in result.vault_notes:
            filename = note.path.split("/")[-1].replace(".md", "")
            preview = note.content[:300]
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{filename}*\n{preview}…"},
            })

    blocks.append({"type": "divider"})
    return blocks


def init_slack_bot() -> None:
    """Initialise le Slack Bolt App si la config est présente."""
    global bolt_app, slack_handler

    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_signing_secret:
        log.info(
            "slack_bot.disabled", reason="SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET not configured"
        )
        return

    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    @app.event("app_mention")
    async def handle_app_mention(event, say, client):
        user_id = event.get("user", "unknown")
        text = event.get("text", "")
        channel = event.get("channel", "")

        log.info("slack.mention", user=user_id, channel=channel, text_len=len(text))

        question = _strip_bot_mention(text)
        if not question:
            await say(text="Pose-moi une question !")
            return

        if _check_rate_limit(user_id):
            await say(text=":hourglass: Trop de requêtes. Attends un instant.")
            return

        with contextlib.suppress(Exception):
            await client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")

        result = await ask_charlie(question, db_path=settings.db_agent_state)
        base_url = settings.public_base_url.rstrip("/") if settings.public_base_url else "https://detective.digitalhs.biz"
        blocks = format_charlie_response(result, base_url)

        try:
            await say(blocks=blocks, text=result.response_text[:200])
        except Exception as e:
            log.warning("slack.send_failed", error=str(e))
            await say(text="Erreur lors de l'envoi de la réponse.")

    @app.event("message")
    async def handle_dm_message(event, say, client):
        if event.get("channel_type") != "im":
            return

        user_id = event.get("user", "unknown")
        text = event.get("text", "").strip()

        log.info("slack.dm", user=user_id, text_len=len(text))

        if not text:
            return

        if _check_rate_limit(user_id):
            await say(text=":hourglass: Trop de requêtes. Attends un instant.")
            return

        with contextlib.suppress(Exception):
            await client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="eyes")

        result = await ask_charlie(text, db_path=settings.db_agent_state)
        base_url = settings.public_base_url.rstrip("/") if settings.public_base_url else "https://detective.digitalhs.biz"
        blocks = format_charlie_response(result, base_url)

        try:
            await say(blocks=blocks, text=result.response_text[:200])
        except Exception as e:
            log.warning("slack.send_failed", error=str(e))
            await say(text="Erreur lors de l'envoi de la réponse.")

    bolt_app = app
    slack_handler = AsyncSlackRequestHandler(app)
    log.info("slack_bot.enabled")
