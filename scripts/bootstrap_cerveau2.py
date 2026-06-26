#!/usr/bin/env python3
"""Migration one-shot : importe l'historique des 3 DB dans Cerveau2-Det.

Usage:
    python -m scripts.bootstrap_cerveau2 --dry-run --limit 20
    python -m scripts.bootstrap_cerveau2
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import httpx
import structlog

from app.cerveau_dossier import derive_dossier_id
from app.config import get_settings

log = structlog.get_logger()

# Mapping catégories DB historiques → Cerveau2 (finesse maximale)
_CATEGORY_MAP: dict[str, str] = {
    "PRISE_CONTACT": "demande_client",
    "INFIDELITE": "infidelite",
    "SURVEILLANCE": "surveillance",
    "ENQUETE_FAMILLE": "enquete_famille",
    "RECHERCHE_PERSONNE": "recherche_personne",
    "FACTURE_DEVIS": "facture",
    "CONTROLE_RESIDENCE": "controle_residence",
    "INVESTIGATION_ENTREPRISE": "investigation_entreprise",
    "AUTRE": "autre",
    "TEST_MATERIEL": "test_materiel",
    "COLLABORATION": "collaboration",
    "HARCELEMENT": "harcelement",
}

# Mapping account → marque (format attendu par Cerveau2)
_ACCOUNT_MARQUE: dict[str, str] = {
    "contact@detectivebelgique.be": "detectivebelgique",
    "contact@detectivebelgium.com": "detectivebelgium",
    "info@dpdhuinvestigations.be": "dpdhu",
    "info@detectives-belgique.be": "detectivesbelgique",
}

_EXCLUDE_CATEGORIES = {"newsletter", "phishing"}


def _marque_from_account(account: str | None, db_name: str = "") -> str:
    if account:
        # Extraire l'email si le format est "Nom <email>"
        import re

        m = re.search(r"<([^>]+)>", account)
        email = m.group(1) if m else account
        return _ACCOUNT_MARQUE.get(email, email)
    # Fallback par nom de DB quand account n'existe pas
    if db_name == "boite2":
        return "detectivebelgium"
    if db_name == "boite3":
        return "dpdhu"
    if db_name == "boite4":
        return "detectivesbelgique"
    return "detectivebelgique"


def _parse_date(date_raw: str | None) -> tuple[str, str]:
    if not date_raw:
        return "", ""
    try:
        dt = datetime.strptime(date_raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        pass
    return date_raw[:10] if len(date_raw) >= 10 else "", ""


def _fetch_emails(db_path: Path, limit: int | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Vérifier si la colonne 'account' existe (pas présente dans boite2/boite3)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(emails)").fetchall()]
    account_col = "account" if "account" in cols else "NULL as account"

    sql = f"""
        SELECT id, imap_uid, folder, date, sender, subject,
               body_preview, body_full, category, anonymized_name,
               {account_col}
        FROM emails
        WHERE category IS NOT NULL
        ORDER BY date
    """
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _fetch_sent_emails(db_path: Path, limit: int | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, imap_uid, date, recipient, subject, body, sender_identity
        FROM sent_emails
        ORDER BY date
    """
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _ingest_email(
    client: httpx.AsyncClient,
    payload: dict,
    base_url: str,
    api_secret: str,
) -> tuple[bool, bool]:
    """Retourne (succès, doublon)."""
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/ingest-email",
            json=payload,
            headers={"Authorization": f"Bearer {api_secret}"},
            timeout=15.0,
        )
        if resp.status_code == 409:
            return True, True
        resp.raise_for_status()
        return True, False
    except httpx.HTTPStatusError as e:
        err_body = e.response.text[:300]
        log.warning(
            "bootstrap.ingest_http_error",
            status=e.response.status_code,
            message_id=payload.get("message_id"),
            error=err_body,
            payload=payload,
        )
        return False, False
    except Exception as e:
        log.warning(
            "bootstrap.ingest_exception",
            error=str(e),
            message_id=payload.get("message_id"),
        )
        return False, False


async def _process_db(
    db_path: Path,
    db_name: str,
    dry_run: bool,
    limit: int | None,
    batch_size: int,
    settings,
) -> dict:
    log.info("bootstrap.start_db", db=db_name, path=str(db_path))

    emails = _fetch_emails(db_path, limit=limit)
    sent = _fetch_sent_emails(db_path, limit=limit)

    stats = {"created": 0, "duplicates": 0, "errors": 0, "skipped": 0}

    async with httpx.AsyncClient() as client:
        # --- Inbox ---
        for idx, row in enumerate(emails):
            cat = row.get("category") or ""
            cat_cerveau = _CATEGORY_MAP.get(cat, cat.lower())
            if cat_cerveau in _EXCLUDE_CATEGORIES:
                stats["skipped"] += 1
                continue

            marque = _marque_from_account(row.get("account"), db_name)
            dossier_id = derive_dossier_id(
                sender=row.get("sender") or "",
                subject=row.get("subject") or "",
                anonymized_name=row.get("anonymized_name"),
                marque=marque,
            )
            date_str, heure_str = _parse_date(row.get("date"))
            body = row.get("body_full") or row.get("body_preview") or ""
            msg_id = row.get("imap_uid") or f"{db_name}_in_{row['id']}"

            payload = {
                "message_id": msg_id,
                "direction": "in",
                "date": date_str,
                "heure": heure_str,
                "expediteur": row.get("sender") or "",
                "destinataire": "daniel",
                "objet": row.get("subject") or "",
                "body": body,
                "marque": marque,
                "dossier_id": dossier_id,
                "categorie": cat_cerveau,
                "zone": "jaune",
                "langue": "fr",
                "priorite": "normal",
            }

            if dry_run:
                log.info(
                    "bootstrap.dry_run",
                    db=db_name,
                    idx=idx,
                    dossier_id=dossier_id,
                    categorie=cat_cerveau,
                    message_id=msg_id,
                )
                continue

            ok, dup = await _ingest_email(
                client, payload, settings.cerveau2_base_url, settings.cerveau2_api_secret
            )
            if dup:
                stats["duplicates"] += 1
            elif ok:
                stats["created"] += 1
            else:
                stats["errors"] += 1

            if (idx + 1) % batch_size == 0:
                log.info("bootstrap.progress", db=db_name, processed=idx + 1, stats=stats)
                await asyncio.sleep(0.5)

        # --- Sent ---
        for idx, row in enumerate(sent):
            marque = _marque_from_account(row.get("sender_identity"), db_name)
            dossier_id = derive_dossier_id(
                sender=row.get("recipient") or "",
                subject=row.get("subject") or "",
                marque=marque,
            )
            date_str, heure_str = _parse_date(row.get("date"))
            body = row.get("body") or ""
            msg_id = row.get("imap_uid") or f"{db_name}_out_{row['id']}"

            payload = {
                "message_id": msg_id,
                "direction": "out",
                "date": date_str,
                "heure": heure_str,
                "expediteur": row.get("sender_identity") or "daniel",
                "destinataire": row.get("recipient") or "",
                "objet": row.get("subject") or "",
                "body": body,
                "marque": marque,
                "dossier_id": dossier_id,
                "categorie": "sent",
                "zone": "jaune",
                "langue": "fr",
                "priorite": "normal",
            }

            if dry_run:
                log.info(
                    "bootstrap.dry_run_sent",
                    db=db_name,
                    idx=idx,
                    dossier_id=dossier_id,
                    message_id=msg_id,
                )
                continue

            ok, dup = await _ingest_email(
                client, payload, settings.cerveau2_base_url, settings.cerveau2_api_secret
            )
            if dup:
                stats["duplicates"] += 1
            elif ok:
                stats["created"] += 1
            else:
                stats["errors"] += 1

            if (idx + 1) % batch_size == 0:
                log.info("bootstrap.progress_sent", db=db_name, processed=idx + 1, stats=stats)
                await asyncio.sleep(0.5)

    log.info("bootstrap.done_db", db=db_name, stats=stats)
    return stats


async def main():
    parser = argparse.ArgumentParser(description="Bootstrap Cerveau2-Det depuis les DB historiques")
    parser.add_argument("--dry-run", action="store_true", help="Logger les payloads sans envoyer")
    parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre d'emails par DB")
    parser.add_argument("--batch-size", type=int, default=50, help="Taille des batchs avec pause")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.cerveau2_base_url or not settings.cerveau2_api_secret:
        log.error("bootstrap.missing_config", base_url=settings.cerveau2_base_url)
        sys.exit(1)

    dbs = [(mb.db_path, f"boite{idx}") for idx, mb in enumerate(settings.mailboxes(), start=1)]

    total = {"created": 0, "duplicates": 0, "errors": 0, "skipped": 0}
    for db_path, db_name in dbs:
        if not db_path.exists():
            log.warning("bootstrap.db_missing", db=db_name, path=str(db_path))
            continue
        stats = await _process_db(
            db_path, db_name, args.dry_run, args.limit, args.batch_size, settings
        )
        for k in total:
            total[k] += stats.get(k, 0)

    log.info("bootstrap.complete", total=total)
    print(f"\n✅ Bootstrap terminé : {total}")


if __name__ == "__main__":
    asyncio.run(main())
