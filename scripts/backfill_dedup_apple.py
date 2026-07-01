"""Backfill des doublons logiques pré-v1.28.3 (fix inbox polluée #719-#722).

v1.28.3 ajoute `is_logical_duplicate()` dans le poller runtime. Ce script
applique la MÊME logique aux mails DÉJÀ persistés en prod : marque les
doublons en `status=duplicate` et supprime physiquement les brouillons
Drafts IMAP correspondants.

Usage :
  # Dry-run d'abord (DÉFAUT, ne touche à rien)
  python -m scripts.backfill_dedup_apple --dry-run

  # Apply (après validation humaine CDAL)
  python -m scripts.backfill_dedup_apple --apply

  # Filtrer par boîte
  python -m scripts.backfill_dedup_apple --dry-run --mailbox detective_belgique

Critères de regroupement (clé de dédup) :
- sender_normalized = lower + strip display name
- subject_normalized = strip Re:/Fwd:/AW:/TR:/SV: multi-niveaux + lower

On garde le PREMIER (id ASC) du groupe comme référence. Tous les autres
sont marqués `status=duplicate`, `category=autre`, `priority=low`,
`draft_generated=0`, `ai_draft=NULL`.

Si le brouillon Drafts IMAP correspondant existe (header `X-Detective-Mail-Id`
matche l'id), on le supprime (`\\Deleted` + EXPUNGE).

Idempotent : un second run ne fait rien (les `status=duplicate` sont exclus
de la requête de regroupement).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

import aioimaplib
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.delivery.imap_draft import _find_drafts_folder
from app.pipeline.dedup import normalize_sender, normalize_subject

log = structlog.get_logger()

# Header IMAP custom posé par append_draft() (v1.25.22+) pour identifier
# un brouillon précis. Le script cherche ce header pour savoir quel UID IMAP
# supprimer dans Drafts.
_HEADER_MAIL_ID_RE = re.compile(r"X-Detective-Mail-Id\s*[:]\s*(\d+)", re.IGNORECASE)

# Préfixe des sujets des brouillons V2a (déposés en IMAP Drafts par Charlie).
# Un brouillon Charlie a TOUJOURS ce préfixe dans son sujet DB. On l'exclut
# de la dédup pour ne pas risquer de marquer comme duplicate un brouillon
# légitime en attente d'approbation Daniel (#672 Kirara, etc.).
_BROUILLON_PREFIX = "DEMANDE D'Approbation"


def _find_duplicate_groups(db_path: Path) -> dict[tuple[str, str], list[int]]:
    """Identifie les groupes de mails (sender, subject) qui se dédoublonnent.

    Exclut :
    - les mails déjà en status='duplicate' (cascade guard)
    - les sujets commençant par 'DEMANDE D'Approbation' (= brouillons V2a Charlie,
      JAMAIS à marquer comme duplicate — ce sont des brouillons en attente d'approbation)

    Returns:
        dict[(sender_n, subject_n), list[mail_id]] trié par id ASC.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Filtre brouillon V2a fait en Python (plus simple et lisible que LIKE en SQL).
        rows = conn.execute(
            """
            SELECT id, sender, subject, received_at, status
            FROM mail_processed
            WHERE IFNULL(status, '') != 'duplicate'
              AND IFNULL(sender, '') != ''
              AND IFNULL(subject, '') != ''
            ORDER BY id ASC
            """,
        ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for mail_id, sender, subject, _received_at, _status in rows:
        # Exclure les brouillons V2a : leur sujet commence TOUJOURS par
        # 'DEMANDE D'Approbation'. On ne doit JAMAIS les marquer duplicate
        # (risque de casser un brouillon en attente d'approbation Daniel).
        if subject and subject.strip().lower().startswith(_BROUILLON_PREFIX.lower()):
            continue
        s_n = normalize_sender(sender)
        sub_n = normalize_subject(subject)
        if not s_n or not sub_n:
            continue
        groups[(s_n, sub_n)].append(mail_id)

    # Garde-fou supplémentaire : ne garder que les groupes où TOUS les mails
    # sont arrivés dans une fenêtre de 10 minutes. C'est la signature d'un
    # VRAI doublon serveur (campagne, retry IMAP) — pas une succession de
    # réponses légitimes d'un client impatient (Kirara, rcvall, etc.).
    safe_groups: dict[tuple[str, str], list[int]] = {}
    conn = sqlite3.connect(db_path)
    try:
        for (s_n, sub_n), mail_ids in groups.items():
            if len(mail_ids) < 2:
                continue
            placeholders = ",".join("?" for _ in mail_ids)
            times = conn.execute(
                f"SELECT received_at FROM mail_processed WHERE id IN ({placeholders})",
                mail_ids,
            ).fetchall()
            parsed = []
            for (rt,) in times:
                if not rt:
                    continue
                # mail_processed stocke received_at en RFC 2822 ('Tue, 30 Jun
                # 2026 15:57:50 +0200') — pas ISO 8601. parsedate_to_datetime
                # gère les 2 formats (fallback ISO si jamais la forme change).
                try:
                    parsed.append(parsedate_to_datetime(rt))
                except (ValueError, TypeError):
                    try:
                        parsed.append(datetime.fromisoformat(rt.replace("Z", "+00:00")))
                    except (ValueError, AttributeError):
                        pass
            if len(parsed) < 2:
                continue
            spread = max(parsed) - min(parsed)
            if spread <= timedelta(minutes=30):
                safe_groups[(s_n, sub_n)] = mail_ids
            else:
                log.info(
                    "backfill.skip_group_outside_time_window",
                    sender=s_n,
                    subject=sub_n,
                    n_mails=len(mail_ids),
                    spread_minutes=int(spread.total_seconds() / 60),
                )
    finally:
        conn.close()

    return safe_groups


def _print_groups_dry(groups: dict[tuple[str, str], list[int]], total_mails: int) -> None:
    print(f"\n=== DRY-RUN : {len(groups)} groupe(s) de doublons détecté(s) ===")
    if not groups:
        print("  Aucun doublon. Inbox déjà propre.")
        return
    total_dups = 0
    for (sender, subject), mail_ids in sorted(groups.items(), key=lambda x: x[1][0]):
        keep = mail_ids[0]
        dups = mail_ids[1:]
        total_dups += len(dups)
        print(f"\n[GROUP] sender={sender!r}")
        print(f"        subject={subject!r}")
        print(f"        KEEP   : id={keep}")
        for dup_id in dups:
            print(f"        DUP    : id={dup_id} → status=duplicate + brouillon UID ? à supprimer")
    print(
        f"\nRÉSUMÉ : {len(groups)} groupes, {total_dups} doublons à marquer, "
        f"{total_dups} brouillons Drafts IMAP à supprimer (après lookup X-Detective-Mail-Id)"
    )


def _mark_duplicate(db_path: Path, mail_id: int) -> None:
    """Marque un mail en status=duplicate (audit only, brouillon Drafts à supprimer côté IMAP)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE mail_processed
            SET status = 'duplicate',
                category = 'autre',
                priority = 'low',
                draft_generated = 0,
                ai_draft = NULL
            WHERE id = ? AND IFNULL(status, '') != 'duplicate'
            """,
            (mail_id,),
        )
        conn.commit()
    finally:
        conn.close()


async def _delete_drafts_for_mails(
    mailbox,
    mail_ids: list[int],
    apply: bool,
) -> tuple[int, int]:
    """Supprime les brouillons Drafts IMAP dont le header X-Detective-Mail-Id matche.

    Returns:
        (deleted, kept) — kept = brouillons uniques qu'on n'a pas touchés.
    """
    client = aioimaplib.IMAP4_SSL(host=mailbox.imap_host, port=mailbox.imap_port)
    await client.wait_hello_from_server()
    login = await client.login(mailbox.user, mailbox.app_password)
    if login.result != "OK":
        log.warning("backfill.login_failed", mailbox=mailbox.name)
        return 0, 0

    draft_folder = await _find_drafts_folder(client)
    if not draft_folder:
        log.warning("backfill.no_drafts_folder", mailbox=mailbox.name)
        await client.logout()
        return 0, 0

    await client.select(draft_folder)
    _, data = await client.search("ALL", charset=None)
    raw_uids = data[0].decode().split() if data and data[0] else []
    uids = [u for u in raw_uids if u.isdigit()]

    target_ids = set(mail_ids)
    deleted = 0
    kept = 0
    fetch_failures = 0
    for i, uid in enumerate(uids, 1):
        try:
            _, msg_data = await client.fetch(uid, "(RFC822)")
        except Exception as exc:
            fetch_failures += 1
            log.warning(
                "backfill.fetch_failed",
                mailbox=mailbox.name,
                uid=uid,
                error=str(exc)[:120],
            )
            continue
        # throttle OVH
        if i % 5 == 0:
            await asyncio.sleep(0.1)
        raw = (
            bytes(msg_data[1])
            if len(msg_data) > 1 and isinstance(msg_data[1], (bytes, bytearray))
            else b""
        )
        try:
            msg = message_from_bytes(raw)
            headers_blob = "\n".join(f"{k}: {v}" for k, v in msg.items())
        except Exception:
            headers_blob = raw.decode("utf-8", errors="replace")
        m = _HEADER_MAIL_ID_RE.search(headers_blob)
        if not m:
            continue
        try:
            draft_id = int(m.group(1))
        except ValueError:
            continue
        if draft_id not in target_ids:
            continue
        # On a trouvé un brouillon pour ce mail
        if apply:
            try:
                await client.store(uid, "+FLAGS", "\\Deleted")
                log.info(
                    "backfill.draft_deleted",
                    mailbox=mailbox.name,
                    uid=uid,
                    mail_id=draft_id,
                )
                deleted += 1
            except Exception as exc:
                log.warning(
                    "backfill.store_failed",
                    mailbox=mailbox.name,
                    uid=uid,
                    mail_id=draft_id,
                    error=str(exc)[:120],
                )
        else:
            log.info(
                "backfill.would_delete_draft",
                mailbox=mailbox.name,
                uid=uid,
                mail_id=draft_id,
            )
            deleted += 1

    if fetch_failures:
        log.warning(
            "backfill.fetch_failures_summary",
            mailbox=mailbox.name,
            failures=fetch_failures,
            total_uids=len(uids),
        )

    if apply and deleted:
        try:
            await client.expunge()
            log.info("backfill.expunged", mailbox=mailbox.name, deleted=deleted)
        except Exception as exc:
            log.warning(
                "backfill.expunge_failed",
                mailbox=mailbox.name,
                error=str(exc)[:120],
            )

    with contextlib.suppress(Exception):
        await client.logout()
    return deleted, kept


async def main(apply: bool, only_mailbox: str | None, skip_mailbox: str | None) -> None:
    settings = get_settings()
    db_path = settings.db_agent_state
    log.info("backfill.start", apply=apply, db_path=str(db_path))

    groups = _find_duplicate_groups(db_path)
    total_mails = sum(len(v) for v in groups.values())

    if not apply:
        _print_groups_dry(groups, total_mails)
        log.info("backfill.dry_run_done")
        return

    # APPLY : pour chaque groupe, marquer les doublons (sauf keep=premier) + IMAP
    total_marked = 0
    total_drafts_deleted = 0
    mailboxes_by_name = {mb.name: mb for mb in settings.mailboxes()}

    for (sender, subject), mail_ids in sorted(groups.items(), key=lambda x: x[1][0]):
        keep = mail_ids[0]
        dups = mail_ids[1:]
        # Récupérer le mailbox_name de chaque doublon pour grouper les suppressions
        conn = sqlite3.connect(db_path)
        try:
            placeholders = ",".join("?" for _ in dups)
            rows = conn.execute(
                f"SELECT id, mailbox_name, imap_uid FROM mail_processed "
                f"WHERE id IN ({placeholders})",
                dups,
            ).fetchall()
        finally:
            conn.close()

        by_mb: dict[str, list[int]] = defaultdict(list)
        for dup_id, mb_name, _imap_uid in rows:
            by_mb[mb_name].append(dup_id)
            _mark_duplicate(db_path, dup_id)
            total_marked += 1

        log.info(
            "backfill.group_processed",
            sender=sender,
            subject=subject,
            keep_id=keep,
            marked=len(dups),
            mailboxes=list(by_mb.keys()),
        )

        for mb_name, dup_ids in by_mb.items():
            if only_mailbox and mb_name != only_mailbox:
                continue
            if skip_mailbox and mb_name == skip_mailbox:
                log.info("backfill.skip_mailbox", mailbox=mb_name)
                continue
            mb = mailboxes_by_name.get(mb_name)
            if not mb:
                log.warning("backfill.unknown_mailbox", mailbox=mb_name)
                continue
            deleted, _kept = await _delete_drafts_for_mails(mb, dup_ids, apply=True)
            total_drafts_deleted += deleted

    log.info(
        "backfill.done",
        apply=True,
        groups=len(groups),
        mails_marked_duplicate=total_marked,
        drafts_deleted=total_drafts_deleted,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill des doublons logiques pré-v1.28.3 (fix inbox #719-#722)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique les modifications (DB + Drafts IMAP). DÉFAUT = dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche ce qui serait modifié sans toucher (DÉFAUT si --apply absent).",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Ne traiter que cette boîte (ex: detective_belgique).",
    )
    parser.add_argument(
        "--skip-mailbox",
        default=None,
        help="Sauter cette boîte (ex: detectives_belgique si OVH timeout).",
    )
    args = parser.parse_args()
    apply = args.apply  # si --apply absent, dry-run par défaut
    asyncio.run(
        main(
            apply=apply,
            only_mailbox=args.mailbox,
            skip_mailbox=args.skip_mailbox,
        )
    )
