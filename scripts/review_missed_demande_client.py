"""Audit périodique : détecte les demandes client "masquées" dans les autres catégories.

Contexte v1.25.12 : tolérance zéro sur les faux négatifs demande_client. Même après
le durcissement du pré-filtre et du classifier, un formulaire WP ou une réponse humaine
peut encore être classé newsletter/autre/facture/rappel à tort. Ce script scanne périodiquement
les mails des catégories non-demandes_client et relance le classifier + les heuristiques.

Usage (dry-run par défaut) :
  python -m scripts.review_missed_demande_client
  python -m scripts.review_missed_demande_client --apply
  python -m scripts.review_missed_demande_client --since 2026-06-01 --limit 50 --apply
  python -m scripts.review_missed_demande_client --lookback-days 14

Planification (session Claude) : 2x/jour, ex: 08h17 et 18h43.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from app.config import MailboxConfig, get_settings
from app.delivery.imap_draft import append_draft
from app.delivery.resend_notifier import IncomingMail
from app.delivery.slack_notifier import send_slack_message
from app.pipeline.classifier import _is_human_followup, _is_reply_to_daniel, classify
from app.pipeline.generator import GenerationResult, generate_draft
from app.pipeline.language import detect_language
from app.pipeline.prefilter import _is_wp_contact_form

log = structlog.get_logger()

_REVIEW_CATEGORIES = ("facture", "newsletter", "spam", "phishing", "rappel", "urgent", "autre")

# Senders système qu'on ne remonte jamais automatiquement.
_SERVICE_SENDERS = (
    "noreply",
    "no-reply",
    "ne-pas-repondre",
    "donotreply",
    "mailer-daemon",
    "infomaniak",
    "google",
    "microsoft",
    "apple",
    "meta",
    "stripe",
    "paypal",
    "amazon",
    "mailchimp",
    "sendinblue",
    "brevo",
    "hubspot",
)

# Senders internes / fournisseurs connus dont une "réponse" n'est pas une demande client.
_INTERNAL_OR_KNOWN_SENDERS = (
    "cdal@digitalhs.biz",
    "info@dpdhuinvestigations.be",
    "maintenance@upartner.agency",
)

# Sujets de notifications automatiques qu'on ne remonte jamais.
_AUTO_SUBJECT_PATTERNS = re.compile(
    r"(?:invitation|updated invitation|invitation updated|accepté|refusé|tentative):",
    re.IGNORECASE,
)

# Sujets purement transactionnels — une réponse là-dessus reste transactionnelle.
_TRANSACTIONAL_SUBJECTS = (
    "facture",
    "invoice",
    "devis",
    "rappel de paiement",
    "relevé",
    "contrat de leasing",
    "cdal_test",
)

# Body contenant des liens de désinscription = spam/newsletter, jamais une demande client.
_UNSUBSCRIBE_MARKERS = (
    "désabonnement",
    "desabonnement",
    "ne plus recevoir",
    "retiré de notre liste",
    "retire de notre liste",
    "unsubscribe",
    "se désinscrire",
    "se desinscrire",
)


def _is_service_sender(sender: str) -> bool:
    s = (sender or "").lower().strip()
    return any(h in s for h in _SERVICE_SENDERS)


def _is_internal_or_known_sender(sender: str) -> bool:
    s = (sender or "").lower().strip()
    return any(h in s for h in _INTERNAL_OR_KNOWN_SENDERS)


def _is_auto_subject(subject: str) -> bool:
    return bool(_AUTO_SUBJECT_PATTERNS.search(subject or ""))


def _is_transactional_subject(subject: str) -> bool:
    s = (subject or "").lower()
    return any(t in s for t in _TRANSACTIONAL_SUBJECTS)


def _has_unsubscribe_marker(body: str) -> bool:
    b = (body or "").lower()
    return any(m in b for m in _UNSUBSCRIBE_MARKERS)


def _parse_date(header_date: str) -> datetime | None:
    """Parse un header Date RFC 2822."""
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(header_date)
    except Exception:
        return None


def _fetch_candidates(db_path: Path, since: str, limit: int | None) -> list[dict]:
    """Récupère les mails non-demandes_client à réexaminer.

    Exclusions :
    - status 'rejected' (Daniel a explicitement refusé).
    - status 'sent' (déjà traité par Daniel / envoyé).
    - draft_generated=1 (déjà reclassifié ou brouillon existant).
    """
    conn = sqlite3.connect(db_path)
    try:
        cats = ",".join("?" * len(_REVIEW_CATEGORIES))
        sql = f"""
            SELECT id, imap_uid, mailbox_name, subject, sender, received_at,
                   category, status, priority, body
            FROM mail_processed
            WHERE category IN ({cats})
              AND received_at >= ?
              AND status NOT IN ('rejected', 'sent')
              AND draft_generated = 0
              AND (ai_draft IS NULL OR ai_draft = '')
            ORDER BY received_at ASC
        """
        params = [*list(_REVIEW_CATEGORIES), since]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        conn.close()


def _find_mailbox(name: str) -> MailboxConfig | None:
    for mb in get_settings().mailboxes():
        if mb.name == name:
            return mb
    return None


def _has_strong_recall_signal(subject: str, body: str, sender: str) -> tuple[bool, str]:
    """Heuristique très conservative pour l'audit périodique.

    On ne relance le LLM que quand on a un signal INCONTESTABLE de demande client
    cachée dans une autre catégorie. L'objectif est de rattraper les formulaires WP
    et les vraies relances/réponses humaines, PAS de reclassifier du spam/newsletter.

    Signaux acceptés :
    1. Formulaire WordPress (body structuré) — incontestable, même sans re-LLM.
    2. Relance/suivi humain — très fort, mais avec garde anti-spam (pas de désinscription).
    3. Réponse à Daniel (citation signée) — seulement si expéditeur externe connu ET
       sujet non transactionnel.

    Exclusions dures : senders de service, senders internes/connus, sujets calendrier,
    sujets transactionnels, body avec opt-out spam.

    IMPORTANT : le formulaire WordPress du propre site est INCONTESTABLE, même s'il
    arrive via un expéditeur technique type noreply/wordpress/mail/contact. On le
    détecte AVANT tout filtre sur l'expéditeur.
    """
    if _is_wp_contact_form(body):
        return True, "wp_contact_form"
    if _is_service_sender(sender):
        return False, "service_sender"
    if _is_internal_or_known_sender(sender):
        return False, "internal_known_sender"
    if _is_auto_subject(subject):
        return False, "auto_subject"
    if _has_unsubscribe_marker(body):
        return False, "unsubscribe_marker"
    if _is_transactional_subject(subject):
        return False, "transactional_subject"
    if _is_human_followup(subject, body, sender):
        return True, "human_followup"
    if _is_reply_to_daniel(body, sender):
        return True, "reply_to_daniel"
    return False, "no_recall_signal"


async def _classify(mail: dict) -> str:
    """Reclassifie via le LLM classifier + post-traitement."""
    return await classify(
        subject=mail["subject"] or "",
        body=mail["body"] or "",
        sender=mail["sender"] or "",
    )


async def _regenerate_and_deliver(
    mail: dict, mailbox: MailboxConfig, apply: bool
) -> tuple[str, str]:
    """Génère le brouillon et le livre en IMAP Drafts. Retourne (new_cat, draft)."""
    body = mail["body"] or ""
    subject = mail["subject"] or ""
    sender = mail["sender"] or ""

    new_cat = await _classify(mail)
    draft = ""

    if new_cat == "demande_client" and apply:
        language = detect_language(body, default=mailbox.default_lang)
        is_followup = _is_reply_to_daniel(body, sender) or _is_human_followup(subject, body, sender)
        result = await generate_draft(
            incoming_subject=subject,
            incoming_body=body,
            sender=sender,
            mailbox=mailbox,
            language=language,
            category=new_cat,
            is_followup_response=is_followup,
        )
        draft = result.draft

        incoming = IncomingMail(
            sender=sender,
            subject=subject,
            body=body,
            received_at=mail["received_at"] or "",
            message_id=mail.get("imap_uid") or "",
        )
        gen = GenerationResult(
            draft=draft,
            raw_draft=draft,
            language=language,
            rag_pairs=[],
            model_used="",
            category=new_cat,
            vault_notes=[],
        )
        ok = await append_draft(incoming, mailbox, gen, mail_id=mail["id"])
        if not ok:
            raise RuntimeError("append_draft returned False")

    return new_cat, draft


def _update_db(db_path: Path, mail_id: int, new_category: str, draft: str, apply: bool) -> None:
    if not apply:
        return
    conn = sqlite3.connect(db_path)
    try:
        if new_category == "demande_client":
            conn.execute(
                """
                UPDATE mail_processed SET
                    category = ?,
                    status = 'pending',
                    priority = 'high',
                    ai_draft = ?,
                    draft_generated = 1,
                    delivered_at = ?
                WHERE id = ?
                """,
                (new_category, draft, datetime.now(UTC).isoformat(), mail_id),
            )
        else:
            conn.execute(
                "UPDATE mail_processed SET category = ? WHERE id = ?",
                (new_category, mail_id),
            )
        conn.commit()
    finally:
        conn.close()


async def _send_slack_summary(found: list[dict], dry_run: bool) -> None:
    """Alerte Slack récapitulative si des mails ont été rattrapés."""
    if not found:
        return
    action = "LIVRÉS" if not dry_run else "DÉTECTÉS (dry-run)"
    lines = []
    for m in found[:10]:
        lines.append(
            f"• #{m['id']} `{m['old_category']}` → `demande_client` | "
            f"{m['sender'][:40]} | {m['subject'][:60]} | raison: {m['reason']}"
        )
    if len(found) > 10:
        lines.append(f"… et {len(found) - 10} autres.")
    text = (
        f":mag: *Audit demandes client masquées* — {len(found)} brouillons {action}\n"
        + "\n".join(lines)
        + "\n_Vérifiez les brouillons dans les IMAP Drafts avant approbation._"
    )
    try:
        await send_slack_message(text)
    except Exception as e:
        log.warning("review.slack_failed", error=str(e))


async def main(apply: bool, since: str, limit: int | None) -> None:
    settings = get_settings()
    log.info(
        "review.start",
        apply=apply,
        since=since,
        limit=limit,
        db=str(settings.db_agent_state),
    )

    candidates = _fetch_candidates(settings.db_agent_state, since, limit)
    log.info("review.candidates", count=len(candidates))

    found: list[dict] = []
    reclassed_other: list[dict] = []
    skipped = 0

    for i, mail in enumerate(candidates, 1):
        # Étape 1 : signal fort suffisant pour justifier le coût LLM.
        has_signal, reason = _has_strong_recall_signal(
            mail["subject"] or "", mail["body"] or "", mail["sender"] or ""
        )
        if not has_signal:
            log.debug(
                "review.no_signal",
                mail_id=mail["id"],
                category=mail["category"],
                reason=reason,
            )
            continue

        # Étape 2 : reclassifier via LLM + post-traitement.
        try:
            new_cat = await _classify(mail)
        except Exception as e:
            log.error("review.classify_error", mail_id=mail["id"], error=str(e))
            skipped += 1
            continue

        old_cat = mail["category"]
        log.info(
            "review.reclassified",
            mail_id=mail["id"],
            old_category=old_cat,
            new_category=new_cat,
            reason=reason,
            sender=(mail["sender"] or "")[:40],
            subject=(mail["subject"] or "")[:60],
        )

        if new_cat == "demande_client":
            mailbox = _find_mailbox(mail["mailbox_name"])
            if mailbox is None:
                log.warning(
                    "review.no_mailbox",
                    mail_id=mail["id"],
                    mailbox_name=mail["mailbox_name"],
                )
                skipped += 1
                continue

            try:
                _, draft = await _regenerate_and_deliver(mail, mailbox, apply)
            except Exception as e:
                log.error("review.draft_error", mail_id=mail["id"], error=str(e))
                skipped += 1
                continue

            _update_db(settings.db_agent_state, mail["id"], new_cat, draft, apply)
            found.append({
                "id": mail["id"],
                "old_category": old_cat,
                "sender": mail["sender"] or "",
                "subject": mail["subject"] or "",
                "reason": reason,
            })
        else:
            # Le signal était fort mais le LLM confirme une autre catégorie.
            # On met à jour si la catégorie a changé, mais on ne génère pas de brouillon.
            if new_cat != old_cat:
                _update_db(settings.db_agent_state, mail["id"], new_cat, "", apply)
                reclassed_other.append({
                    "id": mail["id"],
                    "old_category": old_cat,
                    "new_category": new_cat,
                })

        if i % 10 == 0:
            log.info("review.progress", processed=i, total=len(candidates))

    # Notifications.
    await _send_slack_summary(found, dry_run=not apply)

    log.info(
        "review.done",
        apply=apply,
        candidates=len(candidates),
        demande_client_found=len(found),
        reclassed_other=len(reclassed_other),
        skipped=skipped,
    )

    if not apply:
        log.info(
            "review.dry_run_note",
            message="Aucun changement appliqué. Relancer avec --apply pour corriger.",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Audit périodique des faux négatifs demande_client"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique les reclassifications et livre les brouillons IMAP",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Date de début du scan (format YYYY-MM-DD, default = lookback-days)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Nombre de jours en arrière si --since n'est pas fourni (default 7)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite le nombre de mails scannés",
    )
    args = parser.parse_args()

    # Normalise la date since.
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
    else:
        since_dt = datetime.now(UTC) - timedelta(days=args.lookback_days)
    since_rfc = since_dt.strftime("%a, %d %b %Y %H:%M:%S %z")

    asyncio.run(main(apply=args.apply, since=since_rfc, limit=args.limit))
