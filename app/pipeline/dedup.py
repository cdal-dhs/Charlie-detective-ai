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
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
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
    window_hours: int = 48,
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

    # Fenêtre glissante : received_at - window_hours à received_at
    try:
        if received_at_iso:
            # RFC 2822 → ISO 8601 : on tente isoformat d'abord (déjà ISO),
            # sinon fromisoformat avec gestion du Z suffix.
            iso = received_at_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        else:
            dt = datetime.now(UTC)
    except (ValueError, AttributeError, TypeError):
        # Fallback : pas de fenêtre exploitable → check sur les window_h passées
        dt = datetime.now(UTC)
    cutoff = dt - timedelta(hours=window_hours)
    cutoff_iso = cutoff.isoformat()

    conn = sqlite3.connect(db_path)
    try:
        # Index implicite via PK sur id. Pour 48h et sender connu : ≤ 100 lignes
        # → full scan sender + filtre subject + filtre received_at, < 5ms en pratique.
        row = conn.execute(
            """
            SELECT id FROM mail_processed
            WHERE LOWER(IFNULL(sender, '')) = ?
              AND LOWER(IFNULL(subject, '')) = ?
              AND datetime(received_at) >= datetime(?)
              AND IFNULL(status, '') != 'duplicate'
            ORDER BY id ASC
            LIMIT 1
            """,
            (sender_n, subject_n, cutoff_iso),
        ).fetchone()
        return (row is not None, row[0] if row else None)
    finally:
        conn.close()
