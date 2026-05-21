import structlog
import httpx

from app.config import get_settings

log = structlog.get_logger()

RESEND_ENDPOINT = "https://api.resend.com/emails"


async def alert_ollama_credit_low() -> None:
    """Envoie un email d'alerte quand Ollama Pro est en rate-limit (crédit épuisé)."""
    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("alert.no_resend_key_skip")
        return

    payload = {
        "from": settings.resend_from,
        "to": ["cdal@digitalhs.biz"],
        "subject": "🚨 Charlie AI — Crédit Ollama Pro épuisé",
        "html": (
            "<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:20px;'>"
            "<h2 style='color:#dc2626;'>🚨 Alerte crédit Ollama Pro</h2>"
            "<p>Le modèle principal <strong>gemma4:31b</strong> a retourné une erreur "
            "<strong>429 Rate Limit</strong>.</p>"
            "<p>Charlie a automatiquement basculé sur le fallback OpenRouter, "
            "mais le crédit Ollama Pro est probablement épuisé.</p>"
            "<p><strong>Action requise :</strong> rechargez votre crédit Ollama Pro "
            "sur <a href='https://ollama.com/upgrade'>ollama.com/upgrade</a></p>"
            "<hr style='border:none;border-top:1px solid #ddd;margin:20px 0;'>"
            "<p style='font-size:12px;color:#666;'>"
            "Cette alerte est envoyée une seule fois par session jusqu'à redémarrage."
            "</p></body></html>"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
            r.raise_for_status()
        log.info("alert.ollama_credit_sent", recipient="cdal@digitalhs.biz")
    except Exception as e:
        log.error("alert.ollama_credit_failed", error=str(e))
