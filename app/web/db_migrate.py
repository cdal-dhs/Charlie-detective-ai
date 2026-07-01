from __future__ import annotations

from pathlib import Path

import aiosqlite
import structlog

log = structlog.get_logger()

_DEFAULT_USERS = [
    ("cdal@digitalhs.biz", "super_admin", "CDAL"),
    ("contact@detectivebelgique.be", "operator", "Detective Belgique"),
]

_MAIL_PROCESSED_COLS: list[tuple[str, str]] = [
    ("status", "TEXT"),
    ("priority", "TEXT"),
    ("ai_draft", "TEXT"),
    ("human_draft", "TEXT"),
    ("reviewed_by", "INTEGER"),
    ("reviewed_at", "DATETIME"),
    ("sent_at", "DATETIME"),
    ("sent_by", "INTEGER"),
    ("body_preview", "TEXT"),
    ("body", "TEXT"),
    # v1.25.22 — header Reply-To du mail entrant. Pour les forwarders WP, le
    # vrai email client vit ici (cas #629 : ckremp@vo.lu). Vide sinon.
    ("reply_to", "TEXT"),
    # v1.25.28 — sujet de brouillon lisible (ex. "Investigation successorale —
    # Philippe Boeteman") persisté à la génération. Permet au livreur backfill
    # (deliver_pending_drafts) de livrer un sujet propre au lieu du sujet original
    # (template WP absurde / tag [NO_EMAIL_IN_THE_FORM]). Cf. #643.
    ("suggested_subject", "TEXT"),
    # v1.29.0 — système de fil de discussion (threading) cockpit inbox.
    # Le mail initial (parent) + ses replies ping-pong sont regroupés sous un
    # même thread_id. Le thread_subject canonique = sujet du mail le plus ancien.
    # Le dossier_id est dérivé d'une regex "Dossier Dupont" + fallback ref/hash.
    # Cf. tests/test_threading.py et app/pipeline/threading.py.
    # NOTE: "references" est un mot-clé SQLite → on l'escape avec backticks dans
    # les requêtes SQL. En DDL ALTER TABLE, SQLite accepte le nom nu mais certaines
    # versions refusent — on l'ajoute quand même, la lecture SQL utilise des backticks.
    ("message_id", "TEXT"),
    ("in_reply_to", "TEXT"),
    ("dossier_id", "TEXT"),
    ("thread_id", "TEXT"),
    ("thread_subject", "TEXT"),
    # 'references' ajouté en DDL séparé car c'est un mot-clé SQL.
]


async def migrate(db_path: Path) -> None:
    log.info("db.migrate.start", db_path=str(db_path))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        await _create_tables(db)
        await _add_mail_processed_columns(db)
        await _add_mail_processed_indexes(db)
        await _backfill_threading(db)
        await _seed_default_users(db)

        await db.commit()

    log.info("db.migrate.done")


async def _create_tables(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            role TEXT CHECK(role IN ('super_admin', 'operator')) NOT NULL,
            name TEXT,
            is_active INTEGER DEFAULT 1 NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS magic_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            ip_address TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            ip_address TEXT,
            user_agent TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            is_encrypted INTEGER DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            body TEXT,
            editor_id INTEGER,
            ai_generated INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mail_processed_id, version)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS document_scanned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT UNIQUE NOT NULL,
            dossier_id TEXT,
            marque TEXT,
            titre TEXT,
            format TEXT,
            type TEXT DEFAULT 'document',
            date TEXT,
            size_bytes INTEGER,
            cerveau2_synced INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS email_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_processed_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            extracted_text_preview TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (mail_processed_id) REFERENCES mail_processed(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_attachment_mail_id ON email_attachment(mail_processed_id)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            mailbox_name TEXT,
            details TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_event ON agent_telemetry(event_type, created_at)"
    )


async def _add_mail_processed_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_processed'"
    )
    if not await cursor.fetchone():
        log.info("db.migrate.mail_processed_missing_skip_alter")
        return

    cursor = await db.execute("PRAGMA table_info(mail_processed)")
    rows = await cursor.fetchall()
    existing = {r[1] for r in rows}

    for col_name, col_type in _MAIL_PROCESSED_COLS:
        if col_name not in existing:
            log.info("db.migrate.add_column", table="mail_processed", column=col_name)
            await db.execute(f"ALTER TABLE mail_processed ADD COLUMN {col_name} {col_type}")

    # 'references' est un mot-clé SQL — DDL séparé (SQLite ≥ 3.30 l'accepte aussi
    # en ADD COLUMN, mais on reste safe avec quoting pour toutes les versions).
    if "references" not in existing:
        log.info("db.migrate.add_column", table="mail_processed", column="references")
        await db.execute('ALTER TABLE mail_processed ADD COLUMN "references" TEXT')


async def _add_mail_processed_indexes(db: aiosqlite.Connection) -> None:
    """v1.29.0 — index sur thread_id pour les requêtes fil de discussion cockpit.

    Idempotent (IF NOT EXISTS). Log structuré.
    """
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mail_processed_thread ON mail_processed(thread_id)"
    )
    log.info("db.migrate.index_thread_ready")


async def _backfill_threading(db: aiosqlite.Connection) -> None:
    """v1.29.0 — backfill thread_id/thread_subject pour les mails déjà en base.

    Idempotent : WHERE thread_id IS NULL OR thread_id = ''.
    Isole la logique dans un module pur (threading.py) pour testabilité.
    Pour les ~6k mails historiques, faire un run séparé via
    `python -m scripts.backfill_threading --apply` — c'est plus rapide qu'au migrate.
    """
    from app.pipeline.threading import (
        compute_thread_id,
        derive_dossier_id_threading,
        pick_thread_subject,
    )

    cursor = await db.execute(
        """
        SELECT id, subject, body, sender
        FROM mail_processed
        WHERE thread_id IS NULL OR thread_id = ''
        """
    )
    rows = await cursor.fetchall()
    if not rows:
        log.info("db.migrate.backfill_threading_nothing_to_do")
        return

    log.info("db.migrate.backfill_threading_start", n=len(rows))
    updated = 0
    # Group by computed thread_id pour pick_thread_subject en batch
    by_thread: dict[str, list[tuple[str, str]]] = {}
    updates: list[tuple[str, str, int]] = []  # (thread_id, dossier_id, mail_id)

    for mail_id, subject, body, sender in rows:
        dossier_id = derive_dossier_id_threading(subject or "", body or "", sender or "")
        thread_id = compute_thread_id(dossier_id, sender or "")
        updates.append((thread_id, dossier_id, mail_id))
        by_thread.setdefault(thread_id, []).append((subject or "", ""))

    for thread_id, dossier_id, mail_id in updates:
        await db.execute(
            "UPDATE mail_processed SET thread_id = ?, dossier_id = ? WHERE id = ?",
            (thread_id, dossier_id, mail_id),
        )
        updated += 1

    # thread_subject = sujet du mail le plus ancien du fil.
    # Simplification : on prend le sujet courant de chaque mail (pas de tri
    # précis par received_at ici — fait dans le backfill script séparé qui
    # a accès à plus de contexte). À la première ingérence, _refresh_thread_subject
    # dans le poller fera le bon tri.
    for thread_id, _ in by_thread.items():
        subjects = by_thread[thread_id]
        thread_subject = pick_thread_subject(subjects)
        if thread_subject:
            await db.execute(
                "UPDATE mail_processed SET thread_subject = ? WHERE thread_id = ?",
                (thread_subject, thread_id),
            )

    log.info("db.migrate.backfill_threading_done", updated=updated, threads=len(by_thread))


async def _seed_default_users(db: aiosqlite.Connection) -> None:
    for email, role, name in _DEFAULT_USERS:
        await db.execute(
            """
            INSERT OR IGNORE INTO users (email, role, name, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (email, role, name),
        )
    log.info("db.migrate.seeded_default_users", count=len(_DEFAULT_USERS))
