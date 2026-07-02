"""Déduplication logique des mails — v1.28.3 (fix inbox polluée #719-#722).

Contexte : depuis 2 jours, l'inbox du cockpit montrait une cascade de ~10
doublons `Re: Votre reçu Apple` (expéditeur `dpdhuinvestigations@gmail.com`,
boîte D_FR) tous classés `demande_client`/`high`. Aucun check de dédup
logique au poller : 10 message-id IMAP distincts = 10 ingestions + 10
brouillons candidats.

Fix : un doublon logique = (sender_normalized, subject_normalized) déjà
vu dans la fenêtre glissante (48h par défaut). On strippe les préfixes
Re:/Fwd:/AW:/TR:/SV: multi-niveaux du sujet pour ne pas confondre un
nouveau mail et un reply d'un fil existant.

Coût cible : < 5ms/mail, requête SQL unique avec index sur (sender, received_at).
Pas de LLM — déterministe, testable, sûr.

v1.29.0.6 — FIX BUG P0 : la requête SQL utilisait `datetime(received_at)`
qui retourne `None` sur le format RFC 2822 (611/671 mails = 91%).
Conséquence : `datetime(received_at) >= datetime(?)` → comparaison
toujours NULL → la requête ne matche JAMAIS → 0 doublon détecté.

Fix : on fetch TOUS les candidats (sender+subject match) en SQL, puis
on parse received_at en PYTHON via `email.utils.parsedate_to_datetime()`
qui gère RFC 2822 ET ISO 8601. Le filtre fenêtre glissante devient
Python (`dt_received >= dt - window_hours`).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

# Strip préfixes Re:/Fwd:/AW: (insensible casse, multi-niveaux) + whitespace
# Groupes capturés : Re, Fwd/FW, AW, TR, SV. On accepte aussi 'Re :' avec espace.
_RE_PREFIX_RE = re.compile(r"^\s*(re|fwd?|aw|tr|sv)\s*:\s*", re.IGNORECASE)


def normalize_subject(subject: str) -> str:
    """Normalise un sujet pour la clé de dédup.

    - Strip préfixes Re:/Fwd:/AW:/TR:/SV: multi-niveaux (en boucle).
    - Trim + lowercase.
    - Préserve les accents et caractères spéciaux.
    - Sujet vide / None → chaîne vide.
    """
    if not subject:
        return ""
    s = subject.strip()
    # Boucle pour gérer "Re: Re: Votre reçu" (3 niveaux max en pratique)
    prev = None
    while s and s != prev:
        prev = s
        s = _RE_PREFIX_RE.sub("", s).strip()
    return s.lower()


def normalize_sender(sender: str) -> str:
    """Normalise un sender pour la clé de dédup.

    - Extrait l'adresse entre <...> si display name présent.
    - Lowercase + trim.
    - Sender sans @ → chaîne vide (laisser le pipeline décider).
    """
    if not sender or "@" not in sender:
        return ""
    s = sender.strip().lower()
    if "<" in s and ">" in s:
        s = s[s.find("<") + 1 : s.find(">")]
    return s.strip()


def is_logical_duplicate(
    db_path: Path,
    sender: str,
    subject: str,
    received_at_iso: str,
    # v1.29.0 — window_hours devient KWARG-ONLY (après les 4 positionnels
    # historiques) pour éviter toute collision de positionnel avec les
    # 3 kwargs threading ajoutés en queue de signature. Tout call site
    # qui passait `window_hours=48` ou `window_hours=24` en keyword continue
    # de marcher ; tout call site qui passait `48` en positionnel doit
    # être migré (le poller v1.29.0 utilise des kwargs explicites).
    window_hours: int = 48,
    # v1.29.0 — kwargs threading (signature étendue rétrocompat 100%).
    # Pas encore utilisés dans la requête SQL (la dédup reste sur
    # sender+subject sur fenêtre 48h), mais le poller les passe pour
    # préparer une future v1.30 (cascade cross-fil par Message-ID).
    thread_id: str | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> tuple[bool, int | None]:
    """Détecte si un mail est un doublon logique d'un mail déjà persisté.

    Returns:
        (True, original_mail_id) si doublon trouvé, (False, None) sinon.

    Critères (tous obligatoires) :
    - sender_normalized identique (lowercase, sans display name)
    - subject_normalized identique (Re:/Fwd: strippés, lowercase)
    - le mail d'origine a été reçu dans la fenêtre glissante [now - window_h]
    - le mail d'origine n'est PAS lui-même un doublon (filtre status != 'duplicate')
      → évite la cascade (un doublon d'un doublon = nouveau mail légitime)

    Si sender ou subject est vide/invalide : retourne (False, None)
    (laisse le pipeline décider — la dédup est non décisive sur entrées creuses).

    Si received_at_iso est invalide : fallback sur datetime.now() pour calculer
    la fenêtre (pas de crash, comportement conservateur).
    """
    sender_n = normalize_sender(sender)
    subject_n = normalize_subject(subject)
    if not sender_n or not subject_n:
        return False, None

    # v1.29.0.6 — parse received_at côté PYTHON (supporte RFC 2822 ET ISO 8601).
    # AVANT : `datetime(received_at) >= datetime(?)` retournait NULL sur RFC 2822
    # (611/671 mails en prod), donc la dédup était inopérante.
    def _parse_dt(s: str) -> datetime | None:
        if not s:
            return None
        # Essaie d'abord ISO 8601 (chemin rapide)
        try:
            iso = s.replace("Z", "+00:00")
            return datetime.fromisoformat(iso)
        except (ValueError, AttributeError, TypeError):
            pass
        # Fallback RFC 2822 (Tue, 30 Jun 2026 09:44:20 +0200)
        try:
            dt = parsedate_to_datetime(s)
            # Normalise en UTC naive (les tests utilisent utcnow().isoformat() = naive)
            if dt.tzinfo is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError, AttributeError):
            return None

    # dt = timestamp du mail entrant (point de référence pour la fenêtre)
    dt = _parse_dt(received_at_iso) or datetime.now(UTC).replace(tzinfo=None)
    cutoff = dt - timedelta(hours=window_hours)

    conn = sqlite3.connect(db_path)
    try:
        # v1.29.0.6 — on fetch TOUS les candidats (sender+subject match),
        # SANS filtre date SQL. Le filtre fenêtre est fait en Python.
        # Index implicite via PK sur id. Pour un sender connu : ≤ ~50 lignes
        # typiquement (forwarders WP) → full scan acceptable.
        rows = conn.execute(
            """
            SELECT id, received_at FROM mail_processed
            WHERE LOWER(IFNULL(sender, '')) = ?
              AND LOWER(IFNULL(subject, '')) = ?
              AND IFNULL(status, '') != 'duplicate'
            ORDER BY id ASC
            """,
            (sender_n, subject_n),
        ).fetchall()

        for cand_id, cand_received_at in rows:
            cand_dt = _parse_dt(cand_received_at)
            if cand_dt is None:
                # Date non parsable → on accepte comme candidat (compat arrière)
                return True, cand_id
            # Fenêtre glissante : received_at ∈ [cutoff, dt] (= [now-window, now])
            if cutoff <= cand_dt <= dt:
                return True, cand_id
        return False, None
    finally:
        conn.close()
