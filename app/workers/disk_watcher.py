import asyncio
import shutil
from datetime import datetime
from pathlib import Path

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()

THRESHOLD_PERCENT = 24
CHECK_INTERVAL_MINUTES = 60
RESEND_ENDPOINT = "https://api.resend.com/emails"

# One-shot : n'alerter qu'une seule fois par "crise" jusqu'à retour au vert
_alert_sent = False


def _get_disk_info() -> dict:
    """Retourne l'état du filesystem racine."""
    usage = shutil.disk_usage("/")
    total_gb = usage.total / (1024 ** 3)
    used_gb = usage.used / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    free_pct = (usage.free / usage.total) * 100
    return {
        "total_gb": round(total_gb, 1),
        "used_gb": round(used_gb, 1),
        "free_gb": round(free_gb, 1),
        "free_pct": round(free_pct, 1),
    }


async def _send_alert_email(info: dict) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("disk.no_resend_key")
        return

    html = (
        "<html><body style='font-family:Arial,sans-serif;max-width:600px;'>"
        "<h2 style='color:#dc2626;'>URGENT — Espace disque critique sur detective.digitalhs.biz</h2>"
        "<p>Le serveur VPS Hostinger de l'agent Detective.be approche de la saturation.</p>"
        "<table style='border-collapse:collapse;font-size:14px;width:100%;'>"
        f"<tr><td style='padding:8px;border:1px solid #ccc;'><b>Total</b></td>"
        f"<td style='padding:8px;border:1px solid #ccc;'>{info['total_gb']} GB</td></tr>"
        f"<tr><td style='padding:8px;border:1px solid #ccc;'><b>Utilisé</b></td>"
        f"<td style='padding:8px;border:1px solid #ccc;'>{info['used_gb']} GB</td></tr>"
        f"<tr style='background:#fee2e2;'><td style='padding:8px;border:1px solid #ccc;'>"
        f"<b>Libre</b></td><td style='padding:8px;border:1px solid #ccc;color:#dc2626;'>"
        f"<b>{info['free_gb']} GB ({info['free_pct']}%)</b></td></tr>"
        "</table>"
        "<p style='margin-top:16px;font-size:13px;color:#666;'>"
        "Action recommandée : nettoyer les logs, les backups ou agrandir le disque."
        "</p></body></html>"
    )

    payload = {
        "from": settings.resend_from,
        "to": [settings.draft_recipient],
        "subject": f"URGENT — Espace disque critique ({info['free_pct']}% libre)",
        "html": html,
        "headers": {"X-Detective-Agent-Alert": "disk_critical"},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
            r.raise_for_status()
        log.info("disk.alert_sent", free_pct=info["free_pct"], recipient=settings.draft_recipient)
    except Exception as e:
        log.warning("disk.alert_failed", error=str(e))


async def watch_disk(stop_event: asyncio.Event) -> None:
    """Tâche de fond : vérifie l'espace disque toutes les heures."""
    global _alert_sent
    log = structlog.get_logger()
    interval = CHECK_INTERVAL_MINUTES * 60

    while not stop_event.is_set():
        try:
            info = _get_disk_info()
            log.info("disk.check", **info)

            if info["free_pct"] <= THRESHOLD_PERCENT:
                if not _alert_sent:
                    await _send_alert_email(info)
                    _alert_sent = True
                else:
                    log.info("disk.alert_already_sent", free_pct=info["free_pct"])
            else:
                if _alert_sent:
                    log.info("disk.recovered", free_pct=info["free_pct"])
                _alert_sent = False
        except Exception as e:
            log.warning("disk.check_error", error=str(e))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
