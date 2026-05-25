"""Dépose un brouillon de réponse dans les Drafts IMAP de la boîte source.

V2a — Remplace la livraison Resend pour les brouillons demande_client.
Fallback Resend si APPEND échoue (configuré dans imap_poller.py).
"""

from __future__ import annotations

import contextlib
import re
from email.message import EmailMessage

import structlog
from aioimaplib import aioimaplib

from app.config import MailboxConfig, get_settings
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult

log = structlog.get_logger()

# Ordre de préférence pour la découverte du dossier Drafts
_DRAFT_CANDIDATES = ["Drafts", "INBOX.Drafts", "Brouillons", "INBOX.Brouillons"]

_LIST_RE = re.compile(rb'"([^"]+)"\s*$')


def _parse_list_line(line: bytes) -> str | None:
    """Extrait le nom du dossier depuis une réponse LIST IMAP brute."""
    if not isinstance(line, bytes):
        return None
    # Format typique : b'* LIST (\\HasNoChildren \\Draft) "/" "Drafts"'
    # Le nom du dossier est la dernière chaîne entre guillemets.
    m = _LIST_RE.search(line)
    if m:
        return m.group(1).decode("utf-8", errors="replace")
    return None


async def _find_drafts_folder(client: aioimaplib.IMAP4) -> str | None:
    """Interroge LIST pour trouver le dossier Drafts / Brouillons."""
    list_resp = await client.list("", "*")
    if list_resp.result != "OK":
        log.warning("imap_draft.list_failed", response=str(list_resp.result))
        return None

    folders: list[str] = []
    for line in list_resp.lines or []:
        name = _parse_list_line(line)
        if name:
            folders.append(name)

    # Match exact prioritaire
    for candidate in _DRAFT_CANDIDATES:
        for folder in folders:
            if folder.lower() == candidate.lower():
                log.info("imap_draft.folder_found", folder=folder, match="exact")
                return folder

    # Fallback : contient "draft" ou "brouillon"
    for folder in folders:
        lowered = folder.lower()
        if "draft" in lowered or "brouillon" in lowered:
            log.info("imap_draft.folder_found", folder=folder, match="contains")
            return folder

    log.warning("imap_draft.folder_not_found", folders=folders)
    return None


def _build_draft_body(
    incoming: IncomingMail,
    gen: GenerationResult,
    mail_id: int | None,
    base_url: str,
) -> str:
    """Assemble le corps text/plain du brouillon avec bandeau contextuel."""
    lines = ["⚠️  BROUILLON IA — À RELIRE AVANT ENVOI"]
    if mail_id and base_url:
        lines.append(f"Dossier cockpit : {base_url.rstrip('/')}/app/conversation/{mail_id}")
    lines.append("────────────────────────────────────────")
    lines.append("")
    lines.append("=== MESSAGE ORIGINAL DU CLIENT ===")
    lines.append(f"De : {incoming.sender}")
    lines.append(f"Sujet : {incoming.subject}")
    lines.append("")
    lines.append(incoming.body)
    lines.append("")
    lines.append("────────────────────────────────────────")
    lines.append("=== PROPOSITION DE REPONSE ===")
    lines.append("")
    lines.append(gen.draft)
    return "\n".join(lines)


async def append_draft(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
    mail_id: int | None,
) -> bool:
    """Dépose le brouillon dans les Drafts IMAP de la boîte source.

    Retourne ``True`` si succès, ``False`` si échec (le caller active le fallback Resend).
    """
    settings = get_settings()

    body_text = _build_draft_body(
        incoming, gen, mail_id, settings.public_base_url or ""
    )

    msg = EmailMessage()
    msg["From"] = mailbox.user
    msg["To"] = incoming.sender
    msg["Subject"] = f"PROPOSITION REPONSE : {incoming.subject}"
    msg.set_content(body_text)
    message_bytes = msg.as_bytes()

    client = aioimaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        await client.wait_hello_from_server()

        login_resp = await client.login(mailbox.user, mailbox.app_password)
        if login_resp.result != "OK":
            log.warning(
                "imap_draft.login_failed",
                mailbox=mailbox.name,
                response=str(login_resp.result),
            )
            return False

        drafts_folder = await _find_drafts_folder(client)
        if not drafts_folder:
            return False

        append_resp = await client.append(
            message_bytes,
            mailbox=drafts_folder,
            flags=r"\Draft",
        )
        if append_resp.result != "OK":
            log.warning(
                "imap_draft.append_failed",
                mailbox=mailbox.name,
                folder=drafts_folder,
                response=str(append_resp.result),
            )
            return False

        log.info(
            "imap_draft.ok",
            mailbox=mailbox.name,
            folder=drafts_folder,
            sender=incoming.sender,
            subject=msg["Subject"],
        )
        return True

    except Exception as exc:
        log.warning("imap_draft.failed", mailbox=mailbox.name, error=str(exc))
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.logout()
