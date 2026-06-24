"""Dépose un brouillon de réponse dans les Drafts IMAP de la boîte source.

V2a — Remplace la livraison Resend pour les brouillons demande_client.
Fallback Resend si APPEND échoue (configuré dans imap_poller.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from email.message import EmailMessage

import structlog
from aioimaplib import aioimaplib

from app.config import MailboxConfig, get_settings
from app.delivery.resend_notifier import IncomingMail
from app.pipeline.generator import GenerationResult
from app.pipeline.subject_fixer import mask_forwarder_sender

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


async def _verify_draft_present(
    client: aioimaplib.IMAP4,
    drafts_folder: str,
    mail_id: int | None,
) -> bool:
    """Vérifie que le brouillon SPÉCIFIQUE à mail_id est présent dans Drafts.

    v1.25.22 — recherche par header custom ``X-Detective-Mail-Id`` (précis et
    insensible aux sujets pollués / forwarders WP). Avant on cherchait
    n'importe quel brouillon « DEMANDE », ce qui validait à tort un APPEND dont
    le brouillon n'était en fait jamais indexé (cas #614/#629 : delivered_at set
    mais brouillon absent).

    SELECT + SEARCH HEADER — Infomaniak met parfois 1-2s à indexer un APPEND.
    On retente 3× avec 1s de délai pour absorber cette latence.
    """
    if mail_id is None:
        return False
    try:
        sel = await client.select(drafts_folder)
        if sel.result != "OK":
            return False
        for attempt in range(3):
            # SEARCH HEADER X-Detective-Mail-Id <id> — header ASCII, pas d'accent.
            search_resp = await client.search(f"HEADER X-Detective-Mail-Id {mail_id}")
            if search_resp.result == "OK":
                for line in search_resp.lines or []:
                    if line.strip():
                        return True
            # Pas encore indexé ? On retente après 1s.
            await asyncio.sleep(1)
        return False
    except Exception:
        return False


async def _find_drafts_folder(client: aioimaplib.IMAP4) -> str | None:
    """Trouve le dossier Drafts / Brouillons de la boîte IMAP.

    v1.21.9 : la boîte ``detective_belgique`` d'Infomaniak refuse la commande
    LIST avec pattern (``Error in IMAP command LIST: Invalid pattern``), même
    ``LIST "" "*"``. Le fix tente directement ``SELECT`` sur chaque nom
    candidat — Infomaniak autorise SELECT même quand LIST est bloqué.
    On revient à SELECT INBOX à la fin pour ne pas perturber le poller.
    """
    candidates = list(_DRAFT_CANDIDATES) + [
        "INBOX.Brouillons",
        "INBOX.Drafts",
        "Draft",
        "Brouillon",
    ]
    # Dédupliquer en gardant l'ordre
    seen: set[str] = set()
    unique_candidates: list[str] = []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            unique_candidates.append(c)

    for candidate in unique_candidates:
        try:
            sel = await client.select(candidate)
            if sel.result == "OK":
                log.info(
                    "imap_draft.folder_found",
                    folder=candidate,
                    match="select_probe",
                )
                # Important : revenir à INBOX pour ne pas casser le poller
                # qui s'attend à ce que la mailbox sélectionnée soit INBOX
                with contextlib.suppress(Exception):
                    await client.select("INBOX")
                return candidate
        except Exception as exc:
            log.debug(
                "imap_draft.select_probe_failed",
                folder=candidate,
                error=str(exc),
            )
            continue

    # Fallback ultime : tenter LIST quand même, au cas où d'autres boîtes
    # Infomaniak autorisent encore le pattern matching
    try:
        list_resp = await client.list("", "*")
        if list_resp.result == "OK":
            folders: list[str] = []
            for line in list_resp.lines or []:
                name = _parse_list_line(line)
                if name:
                    folders.append(name)
            for candidate in _DRAFT_CANDIDATES:
                for folder in folders:
                    if folder.lower() == candidate.lower():
                        log.info(
                            "imap_draft.folder_found",
                            folder=folder,
                            match="list_exact",
                        )
                        return folder
            for folder in folders:
                lowered = folder.lower()
                if "draft" in lowered or "brouillon" in lowered:
                    log.info(
                        "imap_draft.folder_found",
                        folder=folder,
                        match="list_contains",
                    )
                    return folder
        else:
            log.warning(
                "imap_draft.list_failed_after_select",
                response=str(list_resp.result),
            )
    except Exception as exc:
        log.warning("imap_draft.list_failed_after_select", error=str(exc))

    log.warning("imap_draft.folder_not_found", candidates=unique_candidates)
    return None


def _build_draft_body(
    incoming: IncomingMail,
    gen: GenerationResult,
    mail_id: int | None,
    base_url: str,
) -> str:
    """Assemble le corps text/plain du brouillon avec bandeau contextuel.

    v1.25.9 — Daniel doit voir immédiatement l'EMAIL #id et l'adresse du client
    en haut du brouillon. Le message original est normalement déjà présent dans
    gen.draft (via draft_renderer.py) ; s'il manque (brouillon legacy), on
    l'injecte depuis incoming.body pour garantir le contexte complet.
    """
    # v1.25.22 — priorité au Reply-To : pour les forwarders WP, le vrai email client
    # est dans le Reply-To (cas #629 : ckremp@vo.lu). On l'affiche à la place du
    # forwarder technique / NO_EMAIL_IN_THE_FORM.
    display_sender = mask_forwarder_sender(incoming.sender, incoming.body, incoming.reply_to)

    lines = ["⚠️  BROUILLON IA — À RELIRE AVANT ENVOI"]
    if mail_id:
        lines.append(f"EMAIL #{mail_id} — {display_sender}")
    else:
        lines.append(f"EMAIL CLIENT — {display_sender}")
    if mail_id and base_url:
        lines.append(f"Dossier cockpit : {base_url.rstrip('/')}/app/conversation/{mail_id}")
    lines.append("────────────────────────────────────────")
    lines.append("")

    draft_has_original = "=== MESSAGE ORIGINAL DU CLIENT ===" in (gen.draft or "")
    original_body = (incoming.body or "").strip()

    # Si le draft ne contient pas déjà le message original, on l'affiche explicitement
    # avant le brouillon proposé. Sinon on laisse draft_renderer le gérer (en fin
    # de draft) pour éviter la duplication.
    if original_body and not draft_has_original:
        lines.append("📧 MAIL ORIGINAL DU CLIENT")
        lines.append(f"De : {display_sender}")
        lines.append(f"Sujet : {incoming.subject}")
        lines.append("────────────────────────────────────────")
        lines.append(original_body)
        lines.append("")
        lines.append("════════════════════════════════════════")
        lines.append("💬 BROUILLON DE RÉPONSE PROPOSÉ")
        lines.append("")

    lines.append(gen.draft)
    return "\n".join(lines)


async def append_draft(
    incoming: IncomingMail,
    mailbox: MailboxConfig,
    gen: GenerationResult,
    mail_id: int | None,
    imap_client: aioimaplib.IMAP4 | None = None,
) -> bool:
    """Dépose le brouillon dans les Drafts IMAP de la boîte source.

    Retourne ``True`` si succès, ``False`` si échec (le caller active le fallback Resend).

    Args:
        imap_client: connexion IMAP existante du poller. Si fournie, on la réutilise
            au lieu d'en ouvrir une nouvelle (évite le rejet Infomaniak pour
            connexions simultanées).
    """
    settings = get_settings()

    body_text = _build_draft_body(incoming, gen, mail_id, settings.public_base_url or "")

    msg = EmailMessage()
    msg["From"] = mailbox.user
    # v1.25.18 — si le sender a été masqué en NO_EMAIL_IN_THE_FORM (forwarder WP sans
    # email client), on ne peut pas mettre cette valeur dans le header To. On fallback
    # sur la boîte elle-même ; c'est un brouillon non envoyé de toute façon.
    msg["To"] = incoming.sender if "@" in incoming.sender else mailbox.user
    # v1.25.1 : si le sujet original est un template WP absurde (formulaire relayé
    # par forwarder), on le remplace par un libellé lisible (cas + nom du client).
    draft_subject = gen.suggested_subject or incoming.subject
    msg["Subject"] = f"DEMANDE D'Approbation - Reponse Demande Client : {draft_subject}"
    # v1.25.22 — marqueur ASCII permettant au réconcilieur 15 min de retrouver
    # ce brouillon précis en IMAP (SEARCH HEADER X-Detective-Mail-Id <id>), même
    # si le sujet est pollué par un forwarder WP (cas #614/#629).
    if mail_id is not None:
        msg["X-Detective-Mail-Id"] = str(mail_id)
    msg.set_content(body_text)
    message_bytes = msg.as_bytes()

    own_client = False
    client = imap_client
    if client is None:
        own_client = True
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
        except Exception as exc:
            log.warning("imap_draft.failed", mailbox=mailbox.name, error=str(exc))
            return False

    try:
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

        # Vérification post-dépôt : confirmer que le brouillon est indexé dans Drafts
        verified = await _verify_draft_present(client, drafts_folder, mail_id)
        if verified:
            log.info(
                "imap_draft.ok",
                mailbox=mailbox.name,
                folder=drafts_folder,
                sender=incoming.sender,
                subject=msg["Subject"],
                verified=True,
            )
        else:
            log.warning(
                "imap_draft.unverified",
                mailbox=mailbox.name,
                folder=drafts_folder,
                sender=incoming.sender,
                subject=msg["Subject"],
                note="APPEND a réussi mais SEARCH n'a pas retrouvé le sujet immédiatement",
            )

        # Si on a emprunté la connexion du poller, il faut re-sélectionner INBOX
        # pour que le prochain fetch du poller fonctionne.
        if not own_client:
            with contextlib.suppress(Exception):
                await client.select("INBOX")

        return True

    except Exception as exc:
        log.warning("imap_draft.failed", mailbox=mailbox.name, error=str(exc))
        return False
    finally:
        if own_client:
            with contextlib.suppress(Exception):
                await client.logout()
