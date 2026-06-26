#!/usr/bin/env python3
"""Backfill historique : ingère les emails des DB SQLite (boite1/2/3/4) dans Cerveau2.

Tourne séquentiellement pour éviter de saturer Cerveau2.
Utilise une table de tracking locale pour reprendre en cas d'interruption.

Usage :
    # Local (Mac de CDAL, si .env et data/ sont là)
    python -m scripts.backfill_historical_to_cerveau2

    # Sur le VPS (docker exec, recommandé pour garder les logs dans le conteneur)
    docker compose exec detective python -m scripts.backfill_historical_to_cerveau2

Le script est CPU-friendly : une seule requête HTTP à la fois, pause 0.5s entre
chaque email. Cerveau2 met 40-120s par email (embeddings), donc le rythme est
imposé par la latence de Cerveau2.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.cerveau_client import _map_priority, _trim_body, feed_correspondance
from app.config import get_settings

log = structlog.get_logger()

# Mapping : nom de fichier DB -> slug marque Cerveau2
DB_MARQUE_MAP = {
    "boite1.sqlite": "detectivebelgique",
    "boite2.sqlite": "detectivebelgium",
    "boite3.sqlite": "dpdhu",
    "boite4.sqlite": "detectivesbelgique",
}

# Mapping depuis colonne account (si présente dans la DB)
ACCOUNT_MAP = {
    "contact@detectivebelgique.be": "detectivebelgique",
    "contact@detectivebelgium.com": "detectivebelgium",
    "contact@dpdh-investigations.be": "dpdhu",
    "info@detectives-belgique.be": "detectivesbelgique",
}

# Catégories à skipper (bruit inutile pour Cerveau2)
SKIP_CATEGORIES = {"newsletter", "phishing", "spam"}


def _init_tracking(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cerveau2_historical_ingested (
            message_id TEXT PRIMARY KEY,
            db_name TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def _already_ingested(db_path: Path, message_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM cerveau2_historical_ingested WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_ingested(db_path: Path, message_id: str, db_name: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO cerveau2_historical_ingested (message_id, db_name) VALUES (?, ?)",
        (message_id, db_name),
    )
    conn.commit()
    conn.close()


def _parse_date_rfc2822(date_str: str | None) -> tuple[str, str]:
    """Parse une date RFC 2822 en (YYYY-MM-DD, HH:MM)."""
    if not date_str:
        return "2000-01-01", "00:00"
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except Exception:
            pass
    return "2000-01-01", "00:00"


# Codes pays / marques à ne PAS considérer comme dossier_id
_EXCLUDED_CODES = {
    "NL",
    "BE",
    "FR",
    "EN",
    "VS",
    "UK",
    "DE",
    "IT",
    "ES",
    "US",
    "AU",
    "CA",
    "EU",
    "AI",
    "API",
    "URL",
    "HTTP",
    "HTTPS",
    "SMS",
    "GSM",
    "TV",
    "CD",
    "DVD",
    "VIP",
    "CEO",
    "SARL",
    "SPRL",
    "SCRL",
    "ASBL",
    "NV",
    "BV",
    "SA",
    "SAS",
    "SASU",
    "EURL",
    "SC",
    "SCOP",
}


def _extract_dossier_id_from_subject(subject: str | None) -> str | None:
    """Extrait un dossier_id du sujet de l'email."""
    if not subject:
        return None
    # Pattern dossier N°X ou N° X
    m = re.search(r"[Dd]ossier\s*[Nn]°\s*([A-Za-z0-9_-]+)", subject)
    if m:
        return m.group(1)
    # Pattern ALL-CAPS code (3-10 chars) — ex: ADF, ODM, ZAVENTEM
    # Exclusion des codes trop courts (2 lettres = pays/marques) et des mots courants
    for m in re.finditer(r"\b([A-Z]{3,10})\b", subject):
        code = m.group(1)
        if code not in _EXCLUDED_CODES:
            return code
    return None


def _detect_langue(subject: str | None, body: str | None) -> str:
    """Détection rapide de la langue depuis le sujet ou le corps."""
    text = (subject or "") + " " + (body or "")
    text_lower = text.lower()
    # NL — mots caractéristiques
    if any(
        w in text_lower
        for w in ("het", "de", "een", "en", "voor", "met", "van", "bij", "naar", "bedankt", "groet")
    ):
        return "nl"
    # EN — mots caractéristiques
    if any(
        w in text_lower
        for w in ("the", "and", "for", "with", "from", "to", "of", "in", "on", "thank", "regards")
    ):
        return "en"
    return "fr"


def _get_body(body_full: str | None, body_preview: str | None) -> str:
    """Retourne le meilleur body disponible, tronqué."""
    body = body_full or body_preview or ""
    return _trim_body(body)


async def _ingest_db(
    db_path: Path,
    db_name: str,
    settings,
    tracking_db: Path,
    limit: int | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Ingestion d'une DB historique dans Cerveau2. Retourne (success, skipped, errors)."""
    if not db_path.exists():
        log.warning("backfill.db_not_found", db=db_name)
        return 0, 0, 0

    # Déterminer les colonnes disponibles (les 3 DB n'ont pas exactement le même schéma)
    conn_check = sqlite3.connect(db_path)
    cursor = conn_check.execute("PRAGMA table_info(emails)")
    columns = {row[1] for row in cursor.fetchall()}
    conn_check.close()

    has_account = "account" in columns
    has_urgency = "urgency" in columns
    has_body_full = "body_full" in columns
    has_body_preview = "body_preview" in columns
    has_imap_uid = "imap_uid" in columns
    has_category = "category" in columns

    # Construire la requête SELECT dynamique
    select_cols = ["id", "date", "sender", "subject"]
    if has_category:
        select_cols.append("category")
    else:
        select_cols.append("NULL as category")
    if has_imap_uid:
        select_cols.append("imap_uid")
    else:
        select_cols.append("id as imap_uid")
    if has_body_full:
        select_cols.append("body_full")
    else:
        select_cols.append("NULL as body_full")
    if has_body_preview:
        select_cols.append("body_preview")
    else:
        select_cols.append("NULL as body_preview")
    if has_account:
        select_cols.append("account")
    else:
        select_cols.append("NULL as account")
    if has_urgency:
        select_cols.append("urgency")
    else:
        select_cols.append("0 as urgency")

    conn = sqlite3.connect(db_path)
    sql = f"SELECT {', '.join(select_cols)} FROM emails ORDER BY id"
    if limit:
        sql += f" LIMIT {limit}"
    cursor = conn.execute(sql)
    rows = cursor.fetchall()
    conn.close()

    total = len(rows)
    log.info("backfill.db_start", db=db_name, total=total)

    marque_default = DB_MARQUE_MAP.get(db_name, "detectivebelgique")

    success = 0
    skipped = 0
    errors = 0

    for idx, row in enumerate(rows):
        # Déballage selon l'ordre des colonnes
        id_, date_str, sender, subject, category = row[:5]
        imap_uid = row[5]
        body_full = row[6]
        body_preview = row[7]
        account = row[8]
        urgency = row[9]

        message_id = imap_uid or f"hist-{db_name}-{id_}"
        if not message_id or message_id.strip() == "":
            message_id = f"hist-{db_name}-{id_}"

        if _already_ingested(tracking_db, message_id):
            skipped += 1
            continue

        # Skip catégories bruit
        cat = (category or "").strip().lower()
        if cat in SKIP_CATEGORIES:
            skipped += 1
            _mark_ingested(tracking_db, message_id, db_name)
            continue

        # Marque
        marque = marque_default
        if account and account in ACCOUNT_MAP:
            marque = ACCOUNT_MAP[account]

        # Date / heure
        date_clean, heure = _parse_date_rfc2822(date_str)

        # Body
        body = _get_body(body_full, body_preview)

        # Dossier_id
        dossier_id = _extract_dossier_id_from_subject(subject) or "GENERAL"

        # Langue
        langue = _detect_langue(subject, body)

        # Priorité
        priorite = _map_priority("high" if urgency else "normal")

        if dry_run:
            log.info(
                "backfill.dry_run",
                db=db_name,
                id=id_,
                message_id=message_id,
                marque=marque,
                dossier_id=dossier_id,
                subject=subject[:60],
            )
            success += 1
            continue

        try:
            await feed_correspondance(
                message_id=message_id,
                direction="in",
                date=date_clean,
                heure=heure,
                expediteur=sender or "inconnu",
                destinataire="detective",
                objet=subject or "Sans sujet",
                body=body,
                marque=marque,
                dossier_id=dossier_id,
                categorie=cat or "demande_client",
                zone="jaune",
                langue=langue,
                priorite=priorite,
                base_url=settings.cerveau2_base_url,
                api_secret=settings.cerveau2_api_secret,
            )
            _mark_ingested(tracking_db, message_id, db_name)
            success += 1
        except Exception as e:
            log.warning(
                "backfill.ingest_failed", db=db_name, id=id_, message_id=message_id, error=str(e)
            )
            errors += 1

        # Progress log
        if (idx + 1) % 50 == 0 or (idx + 1) == total:
            log.info(
                "backfill.progress",
                db=db_name,
                done=idx + 1,
                total=total,
                success=success,
                skipped=skipped,
                errors=errors,
                pct=f"{(idx + 1) / total * 100:.1f}%",
            )

        # Pause CPU-friendly : 0.5s entre chaque email pour laisser respirer Cerveau2.
        # Comme Cerveau2 met 40-120s par email, le rythme réel est dicté par la latence
        # HTTP. La pause supplémentaire garantit qu'on n'envoie jamais 2 requêtes
        # simultanées si Cerveau2 répond vite (ex: doublon = réponse instantanée).
        await asyncio.sleep(0.5)

    log.info("backfill.db_done", db=db_name, success=success, skipped=skipped, errors=errors)
    return success, skipped, errors


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backfill emails historiques vers Cerveau2")
    parser.add_argument(
        "--limit", type=int, default=None, help="Nombre max d'emails à traiter par DB (test mode)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Ne pas ingérer, juste logger ce qui serait fait"
    )
    parser.add_argument(
        "--db", type=str, default=None, help="Traiter une seule DB (ex: boite2.sqlite)"
    )
    args = parser.parse_args()

    settings = get_settings()
    tracking_db = settings.db_agent_state

    _init_tracking(tracking_db)

    data_dir = Path("./data")
    all_dbs = [(data_dir / mb.db_path.name, mb.db_path.name) for mb in settings.mailboxes()]

    if args.db:
        dbs = [(data_dir / args.db, args.db)]
    else:
        dbs = all_dbs

    total_success = 0
    total_skipped = 0
    total_errors = 0

    for db_path, db_name in dbs:
        if not db_path.exists():
            log.warning("backfill.skip", db=db_name, reason="not_found")
            continue
        s, sk, e = await _ingest_db(
            db_path,
            db_name,
            settings,
            tracking_db,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        total_success += s
        total_skipped += sk
        total_errors += e

    log.info(
        "backfill.complete",
        total_success=total_success,
        total_skipped=total_skipped,
        total_errors=total_errors,
    )
    print("\n=== BACKFILL TERMINÉ ===")
    print(f"Succès : {total_success}")
    print(f"Déjà présents / skip : {total_skipped}")
    print(f"Erreurs : {total_errors}")


if __name__ == "__main__":
    asyncio.run(main())
