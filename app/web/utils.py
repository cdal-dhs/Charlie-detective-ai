from __future__ import annotations

import base64
import hashlib
import secrets

import aiosqlite
import structlog
from cryptography.fernet import Fernet

from app.config import get_settings

log = structlog.get_logger()


class FernetManager:
    def __init__(self) -> None:
        settings = get_settings()
        key = settings.web_encryption_key.encode()
        if len(key) != 32:
            key = hashlib.sha256(key).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf_token(token: str) -> bool:
    return isinstance(token, str) and len(token) >= 16


async def audit_log(
    db: aiosqlite.Connection,
    user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    details: str | None,
    ip: str | None,
    user_agent: str | None,
) -> None:
    await db.execute(
        """
        INSERT INTO audit_logs (
            user_id, action, resource_type, resource_id, details,
            ip_address, user_agent, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (user_id, action, resource_type, resource_id, details, ip, user_agent),
    )
    await db.commit()
