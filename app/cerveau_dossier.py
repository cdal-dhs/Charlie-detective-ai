"""Logique de dérivation d'un dossier_id pour Cerveau2-Det.

Un dossier_id est soit un numéro de dossier extrait du sujet (ex: "ADF"),
soit un identifiant client stable dérivé de l'expéditeur ou du nom
anonymisé. Cela garantit que toute la correspondance d'un même client
se retrouve dans un seul dossier Cerveau2.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import structlog

log = structlog.get_logger()

# Numéro de dossier court : 3+ caractères alpha-num majuscules
_DOSSIER_REF_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,})\b")

# Mots courants à ignorer (faux positifs dans les sujets d'emails)
_IGNORE_REFS = frozenset({
    "TEST", "TESTING", "DEMO", "EXAMPLE", "SAMPLE",
    "RE", "FW", "FWD", "R", "TR", "VS", "ND", "N",
    "URGENT", "IMPORTANT", "PRIORITY",
    "HELLO", "BONJOUR", "SALUT", "HI",
})

# Dossiers déjà connus de Cerveau2 — peuvent être référencés explicitement
_KNOWN_PREFIXES = frozenset({"DOSSIER", "AFFAIRE", "PROJET", "ENQUETE", "INVESTIGATION"})


def _slug(text: str) -> str:
    """Normalise un texte en slug ASCII safe."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "_", text).strip("_")
    return text[:80]


def extract_dossier_ref(subject: str) -> str | None:
    """Extraire une référence de dossier du sujet (ex: 'ADF', 'PRJ2024').

    Retourne None si aucune correspondance robuste n'est trouvée.
    """
    if not subject:
        return None

    # 1. Mot-clé explicite + référence
    for prefix in _KNOWN_PREFIXES:
        pattern = re.compile(
            rf"(?:{prefix})[\s:#-]*([A-Z][A-Z0-9]{{2,}})",
            re.IGNORECASE,
        )
        m = pattern.search(subject)
        if m:
            ref = m.group(1).upper()
            log.debug("dossier.ref_from_keyword", ref=ref, subject=subject[:60])
            return ref

    # 2. Référence isolée en majuscules (si elle ressemble à un code)
    m = _DOSSIER_REF_RE.search(subject)
    if m:
        ref = m.group(1)
        # Éviter les faux positifs (RE, FW, FWD, TEST, etc.)
        if ref not in _IGNORE_REFS:
            log.debug("dossier.ref_from_caps", ref=ref, subject=subject[:60])
            return ref

    return None


def derive_dossier_id(
    sender: str,
    subject: str = "",
    anonymized_name: str | None = None,
    marque: str = "detective_belgique",
    date: str | None = None,
) -> str:
    """Dérive un dossier_id stable pour Cerveau2.

    Priorité :
    1. Référence explicite dans le sujet (ex: ADF).
    2. Nom anonymisé du client (le plus stable).
    3. Adresse email de l'expéditeur.

    Args:
        sender: Adresse email complète ou champ From.
        subject: Sujet du mail (optionnel, pour extraire une référence).
        anonymized_name: Nom anonymisé du client (ex: "D*****e").
        marque: Identifiant de la marque (ex: detective_belgique).
        date: Date ISO du mail (optionnel, pour regroupement mensuel).

    Returns:
        Un dossier_id unique et stable pour Cerveau2.
    """
    # 1. Référence de dossier dans le sujet
    ref = extract_dossier_ref(subject)
    if ref:
        return ref

    # 2. Nom anonymisé
    if anonymized_name and anonymized_name.strip():
        slug = _slug(anonymized_name)
        return f"{marque}_{slug}"

    # 3. Expéditeur (partie locale du mail)
    email = sender.strip().lower()
    # Extraire la partie locale avant le @, ou utiliser le domaine entier
    local = email.split("@")[0] if "@" in email else email
    slug = _slug(local)
    if not slug:
        slug = "inconnu"

    return f"{marque}_{slug}"


def derive_dossier_id_from_state(
    mail_id: int,
    db_path: Path,
    marque: str = "detective_belgique",
) -> str | None:
    """Lit agent_state.db pour retrouver sender/subject et dériver le dossier_id.

    Retourne None si le mail_id n'existe pas.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sender, subject FROM mail_processed WHERE id = ?",
            (mail_id,),
        ).fetchone()
        conn.close()
    except Exception:
        log.warning("dossier.state_lookup_failed", mail_id=mail_id, db=db_path)
        return None

    if not row:
        return None

    return derive_dossier_id(
        sender=row["sender"] or "",
        subject=row["subject"] or "",
        marque=marque,
    )
