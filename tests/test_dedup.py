"""Tests TDD v1.28.3 — déduplication logique des mails (fix inbox polluée #719-#722).

Contexte : depuis 2 jours, l'inbox du cockpit montrait une cascade de ~10
doublons `Re: Votre reçu Apple` (expéditeur `dpdhuinvestigations@gmail.com`,
boîte D_FR) tous classés `demande_client`/`high`. Chaque doublon déclenchait
un brouillon fantôme en Drafts IMAP, polluant visuellement l'inbox Daniel
et faussant les compteurs stats.

Cause racine : aucun check de dédup logique au poller. 10 message-id IMAP
distincts = 10 ingestions + 10 brouillons candidats. `is_internal_sender()`
v1.28.2 ne capture pas ce cas (sender brand-mais-pas-officiel = pas
`digitalhs.biz`, local-part ≠ `cdal`/`daniel`).

Fix (v1.28.3) :
- `app/pipeline/dedup.py` : module `is_logical_duplicate(db_path, sender,
  subject, received_at, window_hours=48)` qui retourne (True, original_id)
  si doublon détecté, (False, None) sinon.
- Clé de dédup : `(sender_normalized, subject_normalized)` — strip des
  préfixes `Re:`/`Fwd:`/`AW:`/`TR:`/`SV:` multi-niveaux.
- Injection dans `imap_poller._process_single_mail()` AVANT `quick_classify()`
  → 0 coût LLM, 0 brouillon, flag IMAP posé.
- `status='duplicate'` en DB pour audit (cockpit, réconcilieur).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.pipeline.dedup import (
    is_logical_duplicate,
    normalize_sender,
    normalize_subject,
)

# --- normalize_subject -----------------------------------------------------


def test_normalize_subject_strips_re_prefix() -> None:
    """#722 — 'Re: Votre reçu Apple' → 'votre reçu apple' (clé de dédup)."""
    assert normalize_subject("Re: Votre reçu Apple") == "votre reçu apple"


def test_normalize_subject_strips_multi_re() -> None:
    """Multi-niveaux : 'Re: Re: Re: Truc' → 'truc' (client qui répond 3x)."""
    assert normalize_subject("Re: Re: Re: Truc") == "truc"


def test_normalize_subject_strips_fwd_prefix() -> None:
    """Fwd/FW sont des préfixes de transfert, à stripper pour la dédup."""
    assert normalize_subject("Fwd: Truc") == "truc"
    assert normalize_subject("FW: Truc") == "truc"
    assert normalize_subject("fwd: Truc") == "truc"


def test_normalize_subject_strips_aw_tr_sv() -> None:
    """AW (mailing-list reply), TR (turc), SV (scandinaves) → strip."""
    assert normalize_subject("AW: Truc") == "truc"
    assert normalize_subject("TR: Truc") == "truc"
    assert normalize_subject("SV: Truc") == "truc"


def test_normalize_subject_handles_empty() -> None:
    """Sujet vide ou whitespace-only → chaîne vide."""
    assert normalize_subject("") == ""
    assert normalize_subject("   ") == ""
    assert normalize_subject(None or "") == ""  # type: ignore[arg-type]


def test_normalize_subject_preserves_accents() -> None:
    """Les accents et caractères spéciaux sont préservés (lowercase only)."""
    assert normalize_subject("Demande de rens€ignements") == "demande de rens€ignements"
    assert normalize_subject("Évaluation Filature") == "évaluation filature"


def test_normalize_subject_strips_whitespace_around_re() -> None:
    """'Re :  Truc' (espaces variables) doit aussi être stripé."""
    assert normalize_subject("Re :  Truc") == "truc"
    assert normalize_subject("RE:Truc") == "truc"


# --- normalize_sender ------------------------------------------------------


def test_normalize_sender_extracts_angle_brackets() -> None:
    """'Jean Dupont <jean@dupont.fr>' → 'jean@dupont.fr'."""
    assert normalize_sender("Jean Dupont <jean@dupont.fr>") == "jean@dupont.fr"


def test_normalize_sender_lowercases() -> None:
    """'JEAN@DUPONT.FR' → 'jean@dupont.fr' (lowercase)."""
    assert normalize_sender("JEAN@DUPONT.FR") == "jean@dupont.fr"


def test_normalize_sender_handles_bare() -> None:
    """Bare address (sans display name) → inchangé mais lowercase."""
    assert normalize_sender("client@example.com") == "client@example.com"
    assert normalize_sender("Client@Example.COM") == "client@example.com"


def test_normalize_sender_handles_invalid() -> None:
    """Senders invalides (sans @) → chaîne vide (le pipeline décidera)."""
    assert normalize_sender("") == ""
    assert normalize_sender("not-an-email") == ""
    assert normalize_sender("no-at-sign") == ""


def test_normalize_sender_preserves_display_name_with_spaces() -> None:
    """'Display Name <addr>' → 'addr' (l'angle bracket parsing est strict)."""
    assert (
        normalize_sender("DPDH <dpdhuinvestigations@gmail.com>") == "dpdhuinvestigations@gmail.com"
    )


# --- is_logical_duplicate (intégration SQLite in-memory) ------------------


@pytest.fixture
def db_with_mail(tmp_path: Path) -> Path:
    """Crée une DB agent_state avec un mail parent (1h) pour les tests dédup."""
    db = tmp_path / "agent_state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imap_uid TEXT NOT NULL,
            mailbox_name TEXT NOT NULL,
            subject TEXT, sender TEXT, received_at TEXT,
            category TEXT, draft_generated INTEGER DEFAULT 0,
            status TEXT, priority TEXT, ai_draft TEXT, body_preview TEXT
        );
        """
    )
    # Mail parent : il y a 1h, sender suspect, sujet 'Votre reçu Apple' (sans Re:)
    parent_time = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO mail_processed (imap_uid, mailbox_name, subject, sender, "
        "received_at, category, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "100",
            "detective_belgique",
            "Votre reçu Apple",
            "dpdhuinvestigations@gmail.com",
            parent_time,
            "demande_client",
            "pending",
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_dedup_detects_re_prefix_within_window(db_with_mail: Path) -> None:
    """#722 — Re: + même sender dans 1h = doublon détecté."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is True
    assert orig == 1


def test_dedup_detects_different_display_name_same_address(db_with_mail: Path) -> None:
    """Display name différent mais même adresse = doublon (#722 famille)."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "DPDH Investigations <dpdhuinvestigations@gmail.com>",
        "Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is True
    assert orig == 1


def test_dedup_detects_multi_re_prefix(db_with_mail: Path) -> None:
    """Re: Re: doit aussi dédupliquer (le strip est multi-niveaux)."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is True
    assert orig == 1


def test_dedup_passes_different_subject(db_with_mail: Path) -> None:
    """Sujet différent (même sender, dans la fenêtre) = pas doublon."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Question sur la filature",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is False
    assert orig is None


def test_dedup_passes_outside_window(db_with_mail: Path) -> None:
    """Mail parent > 48h = plus considéré comme doublon (mémoire glissante)."""
    # Mettre le mail parent à 50h
    conn = sqlite3.connect(db_with_mail)
    conn.execute(
        "UPDATE mail_processed SET received_at = ? WHERE id = 1",
        ((datetime.utcnow() - timedelta(hours=50)).isoformat(),),
    )
    conn.commit()
    conn.close()
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is False
    assert orig is None


def test_dedup_passes_different_sender(db_with_mail: Path) -> None:
    """Sender différent, même sujet, dans la fenêtre = pas doublon."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "autre@client.com",
        "Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is False
    assert orig is None


def test_dedup_ignores_already_duplicate(db_with_mail: Path) -> None:
    """Cascade guard : un doublon d'un doublon n'est pas re-marqué (filtre status)."""
    # Marquer le parent en 'duplicate'
    conn = sqlite3.connect(db_with_mail)
    conn.execute("UPDATE mail_processed SET status = 'duplicate' WHERE id = 1")
    conn.commit()
    conn.close()
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is False
    assert orig is None


def test_dedup_handles_empty_inputs(tmp_path: Path) -> None:
    """Sender ou sujet vide = pas de dédup (laisser le pipeline décider)."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE mail_processed (
            id INTEGER PRIMARY KEY, imap_uid TEXT, mailbox_name TEXT,
            subject TEXT, sender TEXT, received_at TEXT,
            category TEXT, status TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    # Sender vide
    is_dup, _ = is_logical_duplicate(db, "", "Re: Test", datetime.utcnow().isoformat())
    assert is_dup is False
    # Sujet vide
    is_dup, _ = is_logical_duplicate(db, "client@example.com", "", datetime.utcnow().isoformat())
    assert is_dup is False


def test_dedup_returns_oldest_first(db_with_mail: Path) -> None:
    """Quand plusieurs candidats matchent, retourner le PLUS ANCIEN (id ASC)."""
    # Ajouter un 2e mail plus récent avec le même sender + sujet
    conn = sqlite3.connect(db_with_mail)
    new_time = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
    conn.execute(
        "INSERT INTO mail_processed (imap_uid, mailbox_name, subject, sender, "
        "received_at, category, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "101",
            "detective_belgique",
            "Re: Votre reçu Apple",
            "dpdhuinvestigations@gmail.com",
            new_time,
            "demande_client",
            "pending",
        ),
    )
    conn.commit()
    conn.close()
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Re: Votre reçu Apple",
        datetime.utcnow().isoformat(),
        window_hours=48,
    )
    assert is_dup is True
    # Le plus ancien (id=1) doit être retourné, pas le récent (id=2)
    assert orig == 1


def test_dedup_falls_back_on_invalid_received_at(db_with_mail: Path) -> None:
    """Si received_at est invalide, fallback sur 'now' pour la fenêtre (pas de crash)."""
    is_dup, orig = is_logical_duplicate(
        db_with_mail,
        "dpdhuinvestigations@gmail.com",
        "Re: Votre reçu Apple",
        "not-a-valid-date",
        window_hours=48,
    )
    # Le parent est à -1h → encore dans la fenêtre de 48h
    assert is_dup is True
    assert orig == 1
