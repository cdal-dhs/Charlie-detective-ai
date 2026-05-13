import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec
import structlog
from sentence_transformers import SentenceTransformer

from app.config import get_settings

log = structlog.get_logger()


@dataclass
class RetrievedPair:
    incoming: str
    response: str
    similarity: float
    metadata: dict


_embedder: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        settings = get_settings()
        log.info("rag.load_embedder", model=settings.embedding_model)
        _embedder = SentenceTransformer(settings.embedding_model)
    return _embedder


def embed(text: str) -> list[float]:
    """e5 attend un préfixe 'query: ' pour les requêtes et 'passage: ' pour les documents."""
    model = _get_embedder()
    return model.encode(f"query: {text}", normalize_embeddings=True).tolist()


def embed_passage(text: str) -> list[float]:
    model = _get_embedder()
    return model.encode(f"passage: {text}", normalize_embeddings=True).tolist()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def retrieve(db_path: Path, query_text: str, top_k: int = 5) -> list[RetrievedPair]:
    """TODO S3 : ajuster aux vrais noms de tables/colonnes après inspection des DB
    existantes. Présume une table `pairs_vec` (rowid → embedding) jointable à une
    table `pairs` (incoming, response, brand, lang, date)."""
    qvec = embed(query_text)
    conn = _connect(db_path)
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
