"""Client HTTP asynchrone pour l'API Cerveau2-Det."""
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()


@dataclass
class VaultNote:
    path: str
    content: str


async def query_vault(
    question: str,
    base_url: str,
    api_secret: str,
    dossier_id: str | None = None,
    limit: int = 3,
) -> list[VaultNote]:
    """Interroge le vault Cerveau2-Det et retourne les notes pertinentes.

    Retourne [] si la configuration est absente ou si le service est indisponible
    (dégradation silencieuse — le générateur fonctionne avec le RAG local seul).
    """
    if not base_url or not api_secret:
        return []

    payload: dict = {"question": question, "limit": limit}
    if dossier_id:
        payload["dossier_id"] = dossier_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/query",
                json=payload,
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()

        status = data.get("status", "")
        if status == "zone_rouge":
            log.info("cerveau.zone_rouge_blocked")
            return []

        notes = [
            VaultNote(path=item["path"], content=item["content"])
            for item in data.get("context", [])
        ]
        log.info("cerveau.query_ok", status=status, notes=len(notes))
        return notes

    except Exception as e:
        log.warning("cerveau.query_failed", error=str(e))
        return []
