"""v1.29.0.6 — Backfill dédup post-fix RFC 2822.

Contexte : la dédup v1.28.3 ne marchait PAS sur le format RFC 2822 (611/671 mails).
Résultat : 180+ doublons non marqués `status='duplicate'` polluent l'inbox.

Ce script :
1. Scanne TOUS les mails (status != 'duplicate')
2. Pour chaque groupe (sender_normalized, subject_normalized) avec ≥ 2 mails
3. Parse received_at en Python (RFC 2822 OU ISO)
4. Marque `status='duplicate'` les mails plus récents (le plus ancien reste original)
5. AJOUTE `duplicate_of=<id original>` pour traçabilité

Usage :
    python -m scripts.backfill_dedup_v1_29_0_6 --dry-run  # preview seul
    python -m scripts.backfill_dedup_v1_29_0_6 --apply     # applique
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path


def normalize_subject(subject: str) -> str:
    if not subject:
        return ""
    s = subject.strip()
    prev = None
    pattern = re.compile(r"^\s*(re|fwd?|aw|tr|sv)\s*:\s*", re.IGNORECASE)
    while s and s != prev:
        prev = s
        s = pattern.sub("", s).strip()
    return s.lower()


def normalize_sender(sender: str) -> str:
    if not sender or "@" not in sender:
        return ""
    s = sender.strip().lower()
    if "<" in s and ">" in s:
        s = s[s.find("<") + 1 : s.find(">")]
    return s.strip()


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        iso = s.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except (ValueError, AttributeError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is not None:
            from datetime import UTC
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError, AttributeError):
        return None


def find_duplicates(
    db_path: Path, window_hours: int = 48
) -> list[tuple[int, int, str, str]]:
    """Retourne [(original_id, dup_id, sender_n, subject_n), ...].

    v1.29.0.6 — applique la même logique que `is_logical_duplicate()` :
    un doublon = (sender_n, subject_n) dans une fenêtre glissante [now - window_h].
    C'est crucial pour ne pas marquer en 'duplicate' des e-Box / notifications
    mensuelles (même sender+subject mais espacées de plusieurs mois).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, sender, subject, received_at, status FROM mail_processed "
        "WHERE IFNULL(status, '') != 'duplicate' ORDER BY id"
    ).fetchall()
    conn.close()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        sender_n = normalize_sender(r["sender"] or "")
        subject_n = normalize_subject(r["subject"] or "")
        if not sender_n or not subject_n:
            continue
        groups[(sender_n, subject_n)].append(r)

    dups = []
    for (sender_n, subject_n), mails in groups.items():
        if len(mails) < 2:
            continue
        # Parse received_at pour tous (une fois)
        with_dt = []
        for m in mails:
            dt = _parse_dt(m["received_at"])
            if dt is None:
                # Date non parsable → skip (pas de base pour la fenêtre)
                continue
            with_dt.append((dt, m))
        with_dt.sort(key=lambda x: x[0])

        # Algorithme : on parcourt chronologiquement, on cherche l'original
        # dans la fenêtre [dt - window_h, dt] pour chaque mail.
        # Le 1er mail = toujours original (rien avant lui dans la fenêtre).
        if not with_dt:
            continue
        original_dt, original = with_dt[0]
        for dt, m in with_dt[1:]:
            # Si le mail est dans la fenêtre [original_dt, original_dt + window_h]
            # de l'original COURANT, c'est un doublon.
            # Sinon : nouveau "original" (l'ancien n'est plus dans la fenêtre).
            delta = (dt - original_dt).total_seconds() / 3600
            if delta <= window_hours:
                dups.append((original["id"], m["id"], sender_n, subject_n))
            else:
                # L'ancien original est sorti de la fenêtre → ce mail devient
                # le nouvel original pour la prochaine comparaison
                original_dt, original = dt, m
    return dups


def apply_dedup(db_path: Path, dups: list[tuple[int, int, str, str]]) -> None:
    conn = sqlite3.connect(db_path)
    # v1.29.0.6 — ajoute colonne duplicate_of si manquante
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mail_processed)").fetchall()}
    if "duplicate_of" not in cols:
        conn.execute("ALTER TABLE mail_processed ADD COLUMN duplicate_of INTEGER")
        # Index pour lookup rapide des doublons
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_processed_dup_of ON mail_processed(duplicate_of)")
    for orig, dup, _, _ in dups:
        conn.execute(
            "UPDATE mail_processed SET status = 'duplicate', duplicate_of = ? WHERE id = ?",
            (orig, dup),
        )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/app/data/agent_state.db"),
        help="Chemin DB (default: /app/data/agent_state.db)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview sans modification"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Applique le backfill (UPDATE)"
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"❌ DB introuvable: {args.db}")
        return

    dups = find_duplicates(args.db)
    print(f"=== {len(dups)} doublons détectés ===")
    if not dups:
        print("Aucun doublon. Rien à faire.")
        return

    # Top groupes
    from collections import Counter
    groups = Counter((s, subj) for _, _, s, subj in dups)
    print("\nTop groupes affectés :")
    for (sender, subj), n in sorted(groups.items(), key=lambda x: -x[1])[:10]:
        print(f"  n={n:3d} | sender={sender[:40]!r}")
        print(f"         | subject={subj[:60]!r}")

    if args.dry_run or not args.apply:
        print("\n(dry-run : aucun UPDATE appliqué. Utilise --apply pour confirmer.)")
        return

    apply_dedup(args.db, dups)
    print(f"\n✅ {len(dups)} mails marqués status='duplicate' + duplicate_of=original_id")


if __name__ == "__main__":
    main()
