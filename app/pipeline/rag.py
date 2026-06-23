import sqlite3
from dataclasses import dataclass
from pathlib import Path

import litellm
import sqlite_vec
import structlog

from app.config import get_settings

log = structlog.get_logger()


@dataclass
class RetrievedPair:
    incoming: str
    response: str
    similarity: float
    metadata: dict


def _embed(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    key = settings.embedding_api_key or settings.openrouter_api_key or None
    response = litellm.embedding(
        model=settings.embedding_model,
        input=texts,
        api_base=settings.embedding_api_base or None,
        api_key=key,
        encoding_format="float",
    )
    return [
        item.embedding if hasattr(item, "embedding") else item["embedding"]
        for item in response.data
    ]


# TODO: les préfixes query:/passage: sont spécifiques au modèle E5.
# Les embeddings existants dans la DB ont été générés AVEC ces préfixes.
# Ne PAS les retirer sans re-booter le bootstrap sur TOUTES les DB.
def embed(text: str) -> list[float]:
    return _embed([f"query: {text}"])[0]


def embed_passage(text: str) -> list[float]:
    return _embed([f"passage: {text}"])[0]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def retrieve(db_path: Path, query_text: str, top_k: int = 5) -> list[RetrievedPair]:
    """Récupère les paires Q/R historiques les plus similaires.

    v1.24.2 : RAG mis en pause par défaut (settings.rag_enabled = False).
    L'approche déterministe (qualification_builder + few-shot Daniel, v1.22.4)
    est plus fiable que le RAG et remplace celui-ci pour la génération des
    brouillons. Le RAG était de plus cassé sur les 3 boîtes depuis le
    2026-05-28 (table pairs vide / inexistante, point de vigilance #1). On
    court-circuite donc l'appel embedding (coût + latence) tant qu'on n'a pas
    re-bootstrappé pairs_vec ET décidé de réactiver le RAG.

    Dégradation silencieuse si la table n'existe pas ou si l'API embedding
    échoue (comportement d'origine conservé).
    """
    if not get_settings().rag_enabled:
        log.info("rag.disabled_skip")
        return []
    try:
        qvec = embed(query_text)
        conn = _connect(db_path)
    except Exception as e:
        log.warning("rag.embed_or_connect_failed", db=str(db_path), error=str(e))
        return []

    try:
        cur = conn.execute(
            """
            SELECT p.incoming, p.response, p.brand, p.lang, p.date, v.distance
            FROM pairs_vec v
            JOIN pairs p ON p.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(qvec), top_k),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        log.warning("rag.table_missing", db=str(db_path), error=str(e))
        return []
    except Exception as e:
        log.warning("rag.query_failed", db=str(db_path), error=str(e))
        return []
    finally:
        conn.close()

    return [
        RetrievedPair(
            incoming=row[0],
            response=row[1],
            similarity=1.0 - row[5],
            metadata={"brand": row[2], "lang": row[3], "date": row[4]},
        )
        for row in rows
    ]
