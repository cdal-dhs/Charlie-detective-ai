"""Worker de résumé quotidien des newsletters.

Interroge agent_state.db pour les mails classifiés 'newsletter' de la veille,
génère un résumé via LLM, et envoie un digest Slack matinal.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import structlog

from app.config import get_settings
from app.delivery.slack_notifier import notify_digest
from app.llm.router import complete

log = structlog.get_logger()


def _fetch_yesterday_newsletters(db_path: Path) -> list[dict]:
    """Récupère les newsletters traitées hier (minuit → minuit)."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            SELECT mailbox_name, subject, sender, received_at, processed_at
            FROM mail_processed
            WHERE category = 'newsletter'
              AND date(processed_at) = ?
            ORDER BY processed_at
            """,
            (yesterday,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def _build_digest_prompt(newsletters: list[dict]) -> str:
    lines = [
        "Voici les newsletters reçues hier. Résume-les en un paragraphe concis en français.",
        "Pour chaque newsletter, donne : expéditeur, sujet, et l'essentiel du contenu (1 phrase).",
        "Ne dépasse pas 300 mots. Tone pro et neutre.\n",
    ]
    for i, n in enumerate(newsletters, 1):
        lines.append(
            f"{i}. [{n['mailbox_name']}] {n['sender']} – {n['subject']}"
        )
    return "\n".join(lines)


async def _generate_digest(newsletters: list[dict]) -> str:
    settings = get_settings()
    if not newsletters:
        return "Aucune newsletter reçue hier."
    prompt = _build_digest_prompt(newsletters)
    summary = await complete(
        model=settings.llm_model_default,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.3,
    )
    return summary.strip()


def _format_digest_text(summary: str, newsletters: list[dict], date_str: str) -> str:
    lines = [f"*Newsletters du {date_str}*", f"> {summary}", ""]
    if newsletters:
        lines.append("*Détail :*")
        for n in newsletters:
            lines.append(f"• [{n['mailbox_name']}] {n['sender']} – {n['subject']}")
    return "\n".join(lines)


async def run_daily_digest() -> None:
    """Point d'entrée principal : exécuter chaque matin (typiquement via cron/CronCreate)."""
    yesterday = date.today() - timedelta(days=1)
    date_str = yesterday.strftime("%d/%m/%Y")
    subject = f"Newsletters du {date_str}"

    settings = get_settings()
    newsletters = _fetch_yesterday_newsletters(settings.db_agent_state)
    log.info("digest.found", count=len(newsletters), date=date_str)

    summary = await _generate_digest(newsletters)
    text = _format_digest_text(summary, newsletters, date_str)
    await notify_digest(subject, text)
