"""Client HTTP asynchrone pour l'API Cerveau2-Det."""
import asyncio
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


async def feed_correspondance(
    *,
    message_id: str,
    direction: str,
    date: str,
    heure: str,
    expediteur: str,
    destinataire: str,
    objet: str,
    body: str,
    marque: str,
    dossier_id: str,
    categorie: str,
    zone: str = "jaune",
    langue: str = "fr",
    priorite: str = "normal",
    base_url: str,
    api_secret: str,
) -> bool:
    """Envoie un email à Cerveau2 via POST /ingest-email.

    Retourne True si l'ingestion a réussi, False sinon.
    Les erreurs sont loguées mais ne lèvent pas d'exception
    (fire-and-forget avec dégradation silencieuse).
    """
    if not base_url or not api_secret:
        return False

    payload = {
        "message_id": message_id,
        "direction": direction,
        "date": date,
        "heure": heure,
        "expediteur": expediteur,
        "destinataire": destinataire,
        "objet": objet,
        "body": body,
        "marque": marque,
        "dossier_id": dossier_id,
        "categorie": categorie,
        "zone": zone,
        "langue": langue,
        "priorite": priorite,
    }

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/ingest-email",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_secret}"},
                )
                resp.raise_for_status()
                data = resp.json()
                log.info(
                    "cerveau.feed_ok",
                    dossier_id=dossier_id,
                    message_id=message_id,
                    created=data.get("created", False),
                )
                return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                # Doublon — considéré comme succès
                log.info("cerveau.feed_duplicate", message_id=message_id, dossier_id=dossier_id)
                return True
            log.warning(
                "cerveau.feed_failed",
                attempt=attempt,
                status=e.response.status_code,
                message_id=message_id,
                dossier_id=dossier_id,
            )
        except Exception as e:
            log.warning(
                "cerveau.feed_error",
                attempt=attempt,
                error=str(e),
                message_id=message_id,
                dossier_id=dossier_id,
            )

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)

    log.error("cerveau.feed_gave_up", message_id=message_id, dossier_id=dossier_id)
    return False
