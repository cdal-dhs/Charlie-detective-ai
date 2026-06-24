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
]


async def migrate(db_path: Path) -> None:
    log.info("db.migrate.start", db_path=str(db_path))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        await _create_tables(db)
        await _add_mail_processed_columns(db)
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
