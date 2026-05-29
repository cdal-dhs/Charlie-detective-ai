"""Client HTTP asynchrone pour l'API Cerveau2-Det."""
import asyncio
import contextlib
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

_MAX_BODY_LEN = 150_000

_PRIORITY_MAP = {"high": "urgent", "normal": "normal", "low": "faible"}


def _map_priority(p: str) -> str:
    return _PRIORITY_MAP.get((p or "").lower().strip(), "normal")


def _trim_body(body: str) -> str:
    if not body or len(body) <= _MAX_BODY_LEN:
        return body or ""
    return body[:_MAX_BODY_LEN] + f"\n\n[... tronqué, taille originale : {len(body)} caractères]"


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
    context_only: bool = True,
) -> tuple[list[VaultNote], str | None]:
    """Interroge le vault Cerveau2-Det.

    Retourne ``(notes, answer)`` où ``answer`` est la réponse générée par le LLM
    interne de Cerveau2 quand ``context_only=False``.  Quand ``context_only=True``
    (défaut pour le générateur de brouillons), ``answer`` vaut ``None``.

    Dégradation silencieuse : retourne ``([], None)`` si le service est indisponible.
    """
    if not base_url or not api_secret:
        return [], None

    payload: dict = {"question": question, "limit": limit, "context_only": context_only}
    if dossier_id:
        payload["dossier_id"] = dossier_id

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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
            return [], None

        notes = [
            VaultNote(path=item["path"], content=item["content"])
            for item in data.get("context", [])
        ]
        answer = data.get("answer") if not context_only else None
        log.info(
            "cerveau.query_ok",
            status=status,
            notes=len(notes),
            has_answer=bool(answer),
            context_only=context_only,
        )
        return notes, answer

    except Exception as e:
        log.warning("cerveau.query_failed", error=str(e), context_only=context_only)
        return [], None


async def get_vault_note(
    path: str,
    base_url: str,
    api_secret: str,
) -> VaultNote | None:
    """Récupère une note du vault par son chemin relatif.

    Retourne ``None`` si le service est indisponible ou la note n'existe pas.
    """
    if not base_url or not api_secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/notes/{path}",
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
        return VaultNote(path=data["path"], content=data["content"])
    except Exception as e:
        log.warning("cerveau.get_note_failed", path=path, error=str(e))
        return None


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

    # Normalisation : Cerveau2 rejette dossier_id vide et priorité "high"/"low"
    dossier_id = (dossier_id or "").strip() or "GENERAL"
    body = _trim_body(body)
    priorite = _map_priority(priorite)

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
            async with httpx.AsyncClient(timeout=120.0) as client:
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
            err_body = ""
            with contextlib.suppress(Exception):
                err_body = e.response.text[:500]
            log.warning(
                "cerveau.feed_failed",
                attempt=attempt,
                status=e.response.status_code,
                message_id=message_id,
                dossier_id=dossier_id,
                response=err_body,
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

    return False


async def feed_document(
    *,
    doc_id: str,
    type: str,
    dossier_id: str,
    marque: str,
    date: str,
    titre: str,
    body: str,
    metadata: dict | None = None,
    zone: str = "jaune",
    langue: str = "fr",
    base_url: str,
    api_secret: str,
) -> bool:
    """Envoie un document à Cerveau2 via POST /ingest-note.

    Même pattern fire-and-forget que feed_correspondance.
    Retourne True si l'ingestion a réussi, False sinon.
    """
    if not base_url or not api_secret:
        return False

    # Normalisation : Cerveau2 rejette dossier_id vide
    dossier_id = (dossier_id or "").strip() or "GENERAL"
    body = _trim_body(body)

    payload = {
        "id": doc_id,
        "type": type,
        "dossier_id": dossier_id,
        "marque": marque,
        "date": date,
        "titre": titre,
        "body": body,
        "metadata": metadata or {},
        "zone": zone,
        "langue": langue,
    }

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/ingest-note",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_secret}"},
                )
                resp.raise_for_status()
                data = resp.json()
                log.info(
                    "cerveau.document_feed_ok",
                    doc_id=doc_id,
                    dossier_id=dossier_id,
                    created=data.get("created", False),
                )
                return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                log.info("cerveau.document_duplicate", doc_id=doc_id, dossier_id=dossier_id)
                return True
            err_body = ""
            with contextlib.suppress(Exception):
                err_body = e.response.text[:500]
            log.warning(
                "cerveau.document_feed_failed",
                attempt=attempt,
                status=e.response.status_code,
                doc_id=doc_id,
                dossier_id=dossier_id,
                response=err_body,
            )
        except Exception as e:
            log.warning(
                "cerveau.document_feed_error",
                attempt=attempt,
                error=str(e),
                doc_id=doc_id,
                dossier_id=dossier_id,
            )

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)

    log.error("cerveau.document_feed_gave_up", doc_id=doc_id, dossier_id=dossier_id)
    return False


async def query_dossiers(
    base_url: str,
    api_secret: str,
    since: str | None = None,
    client_type: str | None = None,
) -> list[dict]:
    """Retourne la liste des dossiers depuis Cerveau2 GET /dossiers (dégradation silencieuse)."""
    if not base_url or not api_secret:
        return []
    params: dict = {}
    if since:
        params["since"] = since
    if client_type:
        params["client_type"] = client_type
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/dossiers",
                headers={"Authorization": f"Bearer {api_secret}"},
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("dossiers", [])
            log.info("cerveau.query_dossiers_ok", total=len(result), since=since)
            return result
    except Exception as e:
        log.warning("cerveau.query_dossiers_failed", error=str(e))
        return []


async def push_correction(
    *,
    question: str,
    corrected_response: str,
    original_response: str = "",
    dossier_id: str | None = None,
    tags: list[str] | None = None,
    base_url: str,
    api_secret: str,
) -> dict | None:
    """Envoie une correction utilisateur à Cerveau2 POST /corrections.

    Retourne {"status": "ok", "path": ...} ou None si indisponible.
    """
    if not base_url or not api_secret:
        return None

    payload = {
        "question": question,
        "corrected_response": corrected_response,
        "original_response": original_response,
        "dossier_id": dossier_id,
        "tags": tags or [],
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/corrections",
                json=payload,
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
            log.info("cerveau.correction_pushed", path=data.get("path"), dossier=dossier_id)
            return data
    except Exception as e:
        log.warning("cerveau.correction_push_failed", error=str(e), dossier=dossier_id)
        return None


async def query_corrections_vault(
    question: str,
    base_url: str,
    api_secret: str,
    dossier_id: str | None = None,
    limit: int = 5,
) -> list[VaultNote]:
    """Recherche les corrections enregistrées dans Cerveau2 GET /corrections.

    Retourne une liste de VaultNote compatibles avec le reste du pipeline.
    """
    if not base_url or not api_secret:
        return []

    params: dict = {"q": question, "limit": limit}
    if dossier_id:
        params["dossier_id"] = dossier_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/corrections",
                params=params,
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            log.info("cerveau.corrections_queried", q=question[:60], hits=len(items))
            return [
                VaultNote(
                    path=item["path"],
                    content=(
                        f"Question : {item['question']}\n\n"
                        f"Réponse corrigée : {item['corrected_response']}\n\n"
                        f"Réponse originale : {item['original_response']}\n\n"
                        f"---\n{item['content']}"
                    ),
                )
                for item in items
            ]
    except Exception as e:
        log.warning("cerveau.corrections_query_failed", error=str(e))
        return []


async def get_backup_status(
    base_url: str,
    api_secret: str,
) -> dict | None:
    """Interroge Cerveau2 sur la date du dernier backup vault.

    Retourne {"status": "ok", "last_backup": "..."} ou None si indisponible.
    """
    if not base_url or not api_secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/admin/backup/status",
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
            log.info("cerveau.backup_status_ok", last_backup=data.get("last_backup"))
            return data
    except Exception as e:
        log.warning("cerveau.backup_status_failed", error=str(e))
        return None
