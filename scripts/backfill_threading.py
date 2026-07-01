"""Backfill des colonnes threading pour les mails pré-v1.29.0.

v1.29.0 ajoute 6 colonnes à `mail_processed` (`message_id`, `in_reply_to`,
`references`, `dossier_id`, `thread_id`, `thread_subject`) + l'index
`idx_mail_processed_thread`. La migration `db_migrate.migrate()` crée
automatiquement les colonnes et l'index, mais ne peut PAS déduire rétroactivement
`dossier_id` / `thread_id` / `thread_subject` à partir des rows existantes
(pas d'accès aux headers IMAP d'origine). Ce script applique la MÊME
logique que `app/pipeline/threading.py` à toutes les rows où
`thread_id IS NULL`.

Usage :
  # Dry-run d'abord (DÉFAUT, ne touche à rien)
  python -m scripts.backfill_threading --dry-run

  # Apply (après validation humaine CDAL)
  python -m scripts.backfill_threading --apply

  # Filtrer par boîte (utile pour debug OVH)
  python -m scripts.backfill_threading --dry-run --mailbox detectives_belgique

Logique de regroupement (clé de fil) :
- `dossier_id` = derive_dossier_id_threading(subject, body, sender)
  (name > ref > hash stable)
- `thread_id` = compute_thread_id(dossier_id, sender)
  = `f"{dossier_id}::{sender_n}"` ou `adhoc::{sender_n}::{hash[:8]}`
- `thread_subject` = pick_thread_subject(rows) = sujet du mail le plus ancien
  du fil (après strip `Re:/Fwd:`).

Idempotent : un second run ne fait rien (les `thread_id NOT NULL` sont
exclus de la requête). Les 4 colonnes `message_id`/`in_reply_to`/
`references` restent NULL (pas d'accès aux headers IMAP d'origine — seul
le runtime poller les capture pour les NOUVEAUX mails).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.pipeline.threading import (
    compute_thread_id,
    derive_dossier_id_threading,
    pick_thread_subject,
)

log = structlog.get_logger()

# Préfixe des sujets des brouillons V2a (déposés en IMAP Drafts par Charlie).
# On les exclut du backfill : leur sujet est un sujet d'approbation
# généré, pas le sujet du fil client (sinon un brouillon en attente
# pourrait voler la place de "parent" dans le fil et fausser
# `thread_subject`).
_BROUILLON_PREFIX = "DEMANDE D'Approbation"


def _find_rows_to_thread(db_path: Path, only_mailbox: str | None) -> list[dict]:
    """Sélectionne les rows où thread_id est NULL et calcule leur thread_id.

    Returns:
        liste de dicts {id, mailbox_name, sender, subject, received_at,
        body_preview, dossier_id, thread_id}.
    """
    conn = sqlite3.connect(db_path)
    try:
        query = """
            SELECT id, mailbox_name, sender, subject, received_at, body
            FROM mail_processed
            WHERE thread_id IS NULL
              AND IFNULL(sender, '') != ''
              AND IFNULL(subject, '') != ''
              AND IFNULL(status, '') != 'duplicate'
        """
        params: tuple = ()
        if only_mailbox:
            query += " AND mailbox_name = ?"
            params = (only_mailbox,)
        query += " ORDER BY id ASC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    for mail_id, mb_name, sender, subject, received_at, body in rows:
        # Exclure les brouillons V2a : leur sujet commence TOUJOURS par
        # 'DEMANDE D'Approbation'. On ne doit JAMAIS les threading
        # (risque de fausser le thread_subject canonique).
        if subject and subject.strip().lower().startswith(_BROUILLON_PREFIX.lower()):
            continue
        dossier_id = derive_dossier_id_threading(subject, body or "", sender)
        thread_id = compute_thread_id(dossier_id, sender)
        results.append(
            {
                "id": mail_id,
                "mailbox_name": mb_name,
                "sender": sender,
                "subject": subject,
                "received_at": received_at,
                "body_preview": (body or "")[:200],
                "dossier_id": dossier_id,
                "thread_id": thread_id,
            }
        )
    return results


def _group_by_thread(rows: list[dict]) -> dict[str, list[dict]]:
    """Regroupe les rows par thread_id."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["thread_id"]].append(r)
    return groups


def _print_groups_dry(groups: dict[str, list[dict]]) -> None:
    print(f"\n=== DRY-RUN : {len(groups)} fil(s) de discussion détecté(s) ===")
    if not groups:
        print("  Aucun fil à backfiller. DB déjà propre.")
        return
    total_mails = 0
    multi_mail_threads = 0
    for thread_id, rows in sorted(groups.items(), key=lambda x: x[1][0]["id"]):
        total_mails += len(rows)
        if len(rows) > 1:
            multi_mail_threads += 1
        oldest = min(rows, key=lambda r: r["received_at"] or "")
        for r in rows:
            marker = "KEEP " if r["id"] == oldest["id"] else "    "
            print(
                f"[{marker}] id={r['id']:>4} mb={r['mailbox_name'][:18]:<18} "
                f"sender={r['sender'][:35]:<35} subject={r['subject'][:50]!r}"
            )
        if len(rows) > 1:
            print(
                f"        → thread_id={thread_id!r}, "
                f"thread_subject={oldest['subject'][:60]!r}, "
                f"dossier_id={oldest['dossier_id']!r}"
            )
    print(
        f"\nRÉSUMÉ : {len(groups)} fils, {total_mails} mails, "
        f"{multi_mail_threads} fils multi-mails (parent + replies)"
    )


def _apply_threading(
    db_path: Path,
    groups: dict[str, list[dict]],
) -> tuple[int, int]:
    """Applique le threading : UPDATE thread_id/dossier_id sur chaque row,
    puis UPDATE thread_subject = sujet du plus ancien sur tout le fil.

    Returns:
        (n_mails_threaded, n_threads_with_subject).
    """
    conn = sqlite3.connect(db_path)
    n_mails = 0
    n_threads = 0
    try:
        for thread_id, rows in groups.items():
            # UPDATE dossier_id + thread_id sur chaque row
            for r in rows:
                conn.execute(
                    """
                    UPDATE mail_processed
                    SET dossier_id = ?, thread_id = ?
                    WHERE id = ? AND thread_id IS NULL
                    """,
                    (r["dossier_id"], thread_id, r["id"]),
                )
                n_mails += 1

            # UPDATE thread_subject sur tout le fil = sujet du plus ancien
            subjects_with_dates = [
                (r["subject"], r["received_at"]) for r in rows
            ]
            canon = pick_thread_subject(subjects_with_dates)
            if canon:
                placeholders = ",".join("?" for _ in rows)
                conn.execute(
                    f"UPDATE mail_processed SET thread_subject = ? "
                    f"WHERE id IN ({placeholders})",
                    [canon, *(r["id"] for r in rows)],
                )
                n_threads += 1
        conn.commit()
    finally:
        conn.close()
    return n_mails, n_threads


def main(apply: bool, only_mailbox: str | None) -> None:
    settings = get_settings()
    db_path = settings.db_agent_state
    log.info("backfill_threading.start", apply=apply, db_path=str(db_path))

    rows = _find_rows_to_thread(db_path, only_mailbox)
    groups = _group_by_thread(rows)

    if not apply:
        _print_groups_dry(groups)
        log.info("backfill_threading.dry_run_done")
        return

    n_mails, n_threads = _apply_threading(db_path, groups)
    log.info(
        "backfill_threading.done",
        apply=True,
        threads=len(groups),
        mails_threaded=n_mails,
        threads_with_subject=n_threads,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill des colonnes threading (dossier_id, thread_id, "
        "thread_subject) pour les mails pré-v1.29.0.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique les UPDATE en DB. DÉFAUT = dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche ce qui serait modifié sans toucher (DÉFAUT si --apply absent).",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Ne traiter que cette boîte (ex: detectives_belgique).",
    )
    args = parser.parse_args()
    apply = args.apply
    main(apply=apply, only_mailbox=args.mailbox)
