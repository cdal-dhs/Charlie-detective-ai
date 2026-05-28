"""One-shot : indexe les paires Q/R historiques dans `pairs_vec` (sqlite-vec)
pour chacune des 3 DB SQLite.

Usage : python -m scripts.bootstrap_embeddings

Le script :
  1. Extrait les paires [email entrant] → [réponse Daniel] depuis les tables
     `emails` et `sent_emails` de chaque DB
  2. Nettoie le body des réponses pour enlever le texte cité
  3. Calcule l'embedding du mail entrant via e5-large (préfixe `passage:`)
  4. Stocke dans `pairs` + `pairs_vec` (sqlite-vec)
"""

import re
import sqlite3
import sys
from pathlib import Path

import sqlite_vec
import structlog

from app.config import Settings, get_settings
from app.pipeline.language import detect_language
from app.pipeline.rag import embed_passage

log = structlog.get_logger()


def _normalize_subject(subject: str | None) -> str:
    if not subject:
        return ""
    s = subject.strip()
    # Enlever les prefixes courants (insensible à la casse, avec/sans espace insécable)
    s = re.sub(r"^(Re|RE|Ré|RÉ|Fwd|FW|TR)\s*[:\s]\s*", "", s, flags=re.IGNORECASE)
    return s.strip().lower()


def _clean_quoted_text(body: str) -> str:
    """Coupe le texte cité (lignes commençant par >)."""
    lines = body.splitlines()
    cleaned = []
    for line in lines:
        if line.strip().startswith(">"):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _parse_rfc_date(date_str: str | None) -> float:
    """Convertit une date RFC 2822 en timestamp (approximatif)."""
    from email.utils import parsedate_tz, mktime_tz

    if not date_str:
        return 0.0
    try:
        tt = parsedate_tz(date_str)
        if tt:
            return mktime_tz(tt)
    except Exception:
        pass
    return 0.0


def _extract_pairs(conn: sqlite3.Connection, brand: str, default_lang: str) -> list[tuple[str, str, str | None, str | None, str | None]]:
    """Extrait les paires Q/R depuis `emails` + `sent_emails`.

    Matching heuristique : sujet normalisé + date antérieure la plus proche.
    """
    # Récupérer tous les emails entrants avec body
    cur = conn.execute(
        "SELECT id, subject, body_full, category, date FROM emails WHERE body_full IS NOT NULL AND body_full != ''"
    )
    emails_raw = cur.fetchall()

    # Récupérer toutes les réponses avec body
    cur = conn.execute(
        "SELECT id, subject, body, date FROM sent_emails WHERE body IS NOT NULL AND body != ''"
    )
    sent_raw = cur.fetchall()

    # Indexer les emails par sujet normalisé
    emails_by_subject: dict[str, list[tuple[int, str, str, str | None, float]]] = {}
    for eid, subject, body_full, category, date in emails_raw:
        norm = _normalize_subject(subject)
        if not norm:
            continue
        ts = _parse_rfc_date(date)
        emails_by_subject.setdefault(norm, []).append((eid, subject, body_full, category, ts))

    pairs: list[tuple[str, str, str | None, str | None, str | None]] = []

    for sid, subject, body, date in sent_raw:
        norm = _normalize_subject(subject)
        if not norm:
            continue
        candidates = emails_by_subject.get(norm, [])
        if not candidates:
            continue

        sent_ts = _parse_rfc_date(date)
        # Chercher l'email avec la date la plus proche et STRICTEMENT antérieure
        best = None
        best_diff = float("inf")
        for eid, _, body_full, category, ts in candidates:
            if ts <= 0 or sent_ts <= 0:
                # Si pas de date parsable, fallback sur l'id (approximation)
                if eid < sid and (best is None or sid - eid < best_diff):
                    best = (eid, body_full, category, date)
                    best_diff = sid - eid
            elif ts < sent_ts:
                diff = sent_ts - ts
                if diff < best_diff:
                    best = (eid, body_full, category, date)
                    best_diff = diff

        if best is None:
            continue

        _, incoming_body, category, response_date = best
        response_clean = _clean_quoted_text(body)
        if not response_clean or len(response_clean) < 20:
            continue

        # Détection langue sur le mail entrant
        lang = detect_language(incoming_body, default=default_lang)  # type: ignore[arg-type]

        pairs.append((incoming_body, response_clean, brand, lang, response_date))

    return pairs


def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    # Supprimer toutes les entrées pairs* du schéma via writable_schema (contourne les locks sqlite-vec)
    existing = conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'pairs%'").fetchall()
    if existing:
        conn.execute("PRAGMA writable_schema=ON")
        try:
            conn.execute("DELETE FROM sqlite_master WHERE name LIKE 'pairs%'")
        finally:
            conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
        # VACUUM pour éliminer les pages orphelines des shadow tables
        conn.execute("VACUUM")

    conn.execute(
        f"CREATE VIRTUAL TABLE pairs_vec USING vec0(embedding float[{dim}])"
    )
    conn.execute(
        """
        CREATE TABLE pairs (
            rowid INTEGER PRIMARY KEY,
            incoming TEXT NOT NULL,
            response TEXT NOT NULL,
            brand TEXT,
            lang TEXT,
            date TEXT
        )
        """
    )
    # Commit critique : sqlite-vec doit avoir ses shadow tables committées avant insertion
    conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def index_db(db_path: Path, brand: str, default_lang: str) -> int:
    log.info("bootstrap.start", db=str(db_path), brand=brand)
    conn = _connect(db_path)
    try:
        pairs = _extract_pairs(conn, brand, default_lang)
        if not pairs:
            log.warning("bootstrap.no_pairs", db=str(db_path))
            return 0

        sample_vec = embed_passage(pairs[0][0])
        _ensure_vec_table(conn, dim=len(sample_vec))

        count = 0
        for incoming, response, brand_, lang, date in pairs:
            cur = conn.execute(
                "INSERT INTO pairs(incoming, response, brand, lang, date) VALUES (?,?,?,?,?)",
                (incoming, response, brand_, lang, date),
            )
            rowid = cur.lastrowid
            vec = embed_passage(incoming)
            conn.execute(
                "INSERT INTO pairs_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(vec)),
            )
            count += 1
        conn.commit()
        log.info("bootstrap.done", db=str(db_path), indexed=count, brand=brand)
        return count
    finally:
        conn.close()


def main() -> None:
    settings = get_settings()
    total = 0
    for mbox in settings.mailboxes():
        if not mbox.db_path.exists():
            log.warning("bootstrap.skip_missing", db=str(mbox.db_path))
            continue
        total += index_db(mbox.db_path, mbox.brand, mbox.default_lang)
    log.info("bootstrap.summary", total_indexed=total)


if __name__ == "__main__":
    sys.exit(main())
