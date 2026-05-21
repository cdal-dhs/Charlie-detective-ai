"""Mémoire persistante de Charlie — garde-fou de la grande bibliothèque."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger()

# ── Schéma ───────────────────────────────────────────────────────────────────
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS charlie_memory (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id          TEXT,
    category            TEXT,
    question            TEXT NOT NULL,
    response            TEXT NOT NULL,
    tags                TEXT,
    feedback            TEXT,  -- 'good' | 'bad' | NULL
    corrected_response  TEXT,
    created_at          TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_charlie_memory_dossier ON charlie_memory(dossier_id);
CREATE INDEX IF NOT EXISTS idx_charlie_memory_created ON charlie_memory(created_at);
"""

# ── Verbes d'enregistrement ──────────────────────────────────────────────────
_SAVE_VERBS = re.compile(
    r"\b(retiens|enregistre|note|sauvegarde|archive|memorise|"
    r"souviens-toi|rappelle-toi|garde|stocke)\b",
    re.IGNORECASE,
)

_QUERY_VERBS = re.compile(
    r"\b(rappelle|souviens|rappelez|retiens|quoi|info|information|"
    r"detail|dernier|precedent|historique|resume)\b",
    re.IGNORECASE,
)


@dataclass
class MemoryEntry:
    id: int
    dossier_id: str | None
    category: str | None
    question: str
    response: str
    tags: str | None
    created_at: str


async def init_memory_table(db_path: Path) -> None:
    """Crée la table charlie_memory si absente, migre si besoin."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_INIT_SQL)
        # Migration S2/S3 : ajouter feedback et corrected_response si absents
        cursor = await db.execute("PRAGMA table_info(charlie_memory)")
        rows = await cursor.fetchall()
        existing = {r[1] for r in rows}
        if "feedback" not in existing:
            await db.execute("ALTER TABLE charlie_memory ADD COLUMN feedback TEXT")
            log.info("charlie_memory.migrate_add_feedback")
        if "corrected_response" not in existing:
            await db.execute("ALTER TABLE charlie_memory ADD COLUMN corrected_response TEXT")
            log.info("charlie_memory.migrate_add_corrected")
        # Index feedback créé APRÈS migration pour éviter OperationalError
        # si la table existe déjà sans la colonne
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_charlie_memory_feedback ON charlie_memory(feedback)"
        )
        await db.commit()
    log.info("charlie_memory.init_ok")


async def save_feedback(
    db_path: Path,
    question: str,
    response: str,
    feedback: str,
    corrected_response: str | None = None,
    dossier_id: str | None = None,
) -> int:
    """Enregistre un feedback (good/bad) sur une réponse de Charlie."""
    tags = ",".join(re.findall(r"[A-Z][A-Z0-9]{2,}", question)) or None

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO charlie_memory (dossier_id, question, response, tags, feedback, corrected_response)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (dossier_id, question, response, tags, feedback, corrected_response),
        )
        await db.commit()
        row_id = cursor.lastrowid

    log.info(
        "charlie_memory.feedback_saved",
        row_id=row_id,
        feedback=feedback,
        dossier_id=dossier_id,
    )
    return row_id


async def get_good_memories(
    db_path: Path,
    question: str,
    limit: int = 3,
) -> list[MemoryEntry]:
    """Retourne les souvenirs validés (feedback=good) les plus pertinents."""
    q_norm = _normalize(question)
    words = [w for w in q_norm.split() if len(w) > 3]

    async with aiosqlite.connect(db_path) as db:
        if words:
            like_template = "question LIKE ? OR response LIKE ? OR tags LIKE ?"
            like_clauses = " OR ".join([like_template] * len(words))
            params = []
            for w in words:
                params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
            params.append(limit)

            rows = await db.execute(
                f"""
                SELECT id, dossier_id, category, question, response, tags, created_at
                FROM charlie_memory
                WHERE feedback = 'good' AND ({like_clauses})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            )
            results = await rows.fetchall()
            return [_row_to_entry(r) for r in results]
    return []


async def save_memory(
    db_path: Path,
    question: str,
    response: str,
    dossier_id: str | None = None,
    category: str | None = None,
) -> int:
    """Sauvegarde un souvenir dans la mémoire de Charlie."""
    # Extraire des tags automatiques (mots en majuscules, références)
    tags = ",".join(re.findall(r"[A-Z][A-Z0-9]{2,}", question)) or None

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO charlie_memory (dossier_id, category, question, response, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dossier_id, category, question, response, tags),
        )
        await db.commit()
        row_id = cursor.lastrowid

    log.info(
        "charlie_memory.saved",
        row_id=row_id,
        dossier_id=dossier_id,
        category=category,
        tags=tags,
    )
    return row_id


async def query_memory(
    db_path: Path,
    question: str,
    dossier_id: str | None = None,
    limit: int = 5,
) -> list[MemoryEntry]:
    """Recherche des souvenirs pertinents pour la question courante."""
    # Normaliser la question pour la recherche
    q_norm = _normalize(question)
    words = [w for w in q_norm.split() if len(w) > 3]

    async with aiosqlite.connect(db_path) as db:
        if dossier_id:
            # Priorité au dossier exact
            rows = await db.execute(
                """
                SELECT id, dossier_id, category, question, response, tags, created_at
                FROM charlie_memory
                WHERE dossier_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (dossier_id, limit),
            )
            results = await rows.fetchall()
            if results:
                return [_row_to_entry(r) for r in results]

        # Sinon recherche par mots-clés dans question/response/tags
        if words:
            like_template = "question LIKE ? OR response LIKE ? OR tags LIKE ?"
            like_clauses = " OR ".join([like_template] * len(words))
            params = []
            for w in words:
                params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
            params.append(limit)

            rows = await db.execute(
                f"""
                SELECT id, dossier_id, category, question, response, tags, created_at
                FROM charlie_memory
                WHERE {like_clauses}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            )
            results = await rows.fetchall()
            return [_row_to_entry(r) for r in results]

    return []


def is_save_request(question: str) -> bool:
    """Détecte si Daniel demande d'enregistrer une information."""
    return bool(_SAVE_VERBS.search(question))


def is_memory_query(question: str) -> bool:
    """Détecte si Daniel demande de se souvenir d'une information."""
    return bool(_QUERY_VERBS.search(question))


def _normalize(text: str) -> str:
    from unicodedata import normalize
    return normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _row_to_entry(row: tuple) -> MemoryEntry:
    return MemoryEntry(
        id=row[0],
        dossier_id=row[1],
        category=row[2],
        question=row[3],
        response=row[4],
        tags=row[5],
        created_at=row[6],
    )
